# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from os import path
from textwrap import dedent
from typing import Iterator

# Re-exporting BuiltDockerImage here, as it has its natural home here, but has moved out to resolve
# a dependency cycle from docker_build_context.
from pants.backend.docker.package_types import BuiltDockerImage as BuiltDockerImage
from pants.backend.docker.registries import DockerRegistries
from pants.backend.docker.subsystems.docker_options import DockerOptions
from pants.backend.docker.target_types import (
    DockerBuildOptionFieldMixin,
    DockerImageSourceField,
    DockerImageTagsField,
    DockerImageTargetStageField,
    DockerRegistriesField,
    DockerRepositoryField,
)
from pants.backend.docker.util_rules.docker_binary import DockerBinary
from pants.backend.docker.util_rules.docker_build_context import (
    DockerBuildContext,
    DockerBuildContextRequest,
)
from pants.backend.docker.utils import format_rename_suggestion
from pants.backend.docker.value_interpolation import (
    DockerInterpolationContext,
    DockerInterpolationError,
)
from pants.core.goals.package import BuiltPackage, PackageFieldSet
from pants.core.goals.run import RunFieldSet
from pants.engine.addresses import Address
from pants.engine.process import FallibleProcessResult, Process, ProcessExecutionFailure
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import Target, WrappedTarget
from pants.engine.unions import UnionRule
from pants.option.global_options import GlobalOptions, ProcessCleanupOption
from pants.util.strutil import bullet_list

logger = logging.getLogger(__name__)


class DockerImageTagValueError(DockerInterpolationError):
    pass


class DockerRepositoryNameError(DockerInterpolationError):
    pass


class DockerBuildTargetStageError(ValueError):
    pass


class DockerImageOptionValueError(ValueError):
    pass


@dataclass(frozen=True)
class DockerFieldSet(PackageFieldSet, RunFieldSet):
    required_fields = (DockerImageSourceField,)

    registries: DockerRegistriesField
    repository: DockerRepositoryField
    source: DockerImageSourceField
    tags: DockerImageTagsField
    target_stage: DockerImageTargetStageField

    def format_tag(self, tag: str, interpolation_context: DockerInterpolationContext) -> str:
        source = DockerInterpolationContext.TextSource(
            address=self.address, target_alias="docker_image", field_alias=self.tags.alias
        )
        return interpolation_context.format(
            tag, source=source, error_cls=DockerImageTagValueError
        ).lower()

    def format_repository(
        self, default_repository: str, interpolation_context: DockerInterpolationContext
    ) -> str:
        repository_context = DockerInterpolationContext.from_dict(
            {
                "directory": path.basename(self.address.spec_path),
                "name": self.address.target_name,
                "parent_directory": path.basename(path.dirname(self.address.spec_path)),
                **interpolation_context,
            }
        )
        if self.repository.value:
            repository_text = self.repository.value
            source = DockerInterpolationContext.TextSource(
                address=self.address, target_alias="docker_image", field_alias=self.repository.alias
            )
        else:
            repository_text = default_repository
            source = DockerInterpolationContext.TextSource(
                options_scope="[docker].default_repository"
            )
        return repository_context.format(
            repository_text, source=source, error_cls=DockerRepositoryNameError
        ).lower()

    def image_refs(
        self,
        default_repository: str,
        registries: DockerRegistries,
        interpolation_context: DockerInterpolationContext,
    ) -> tuple[str, ...]:
        """The image refs are the full image name, including any registry and version tag.

        In the Docker world, the term `tag` is used both for what we here prefer to call the image
        `ref`, as well as for the image version, or tag, that is at the end of the image name
        separated with a colon. By introducing the image `ref` we can retain the use of `tag` for
        the version part of the image name.

        Returns all image refs to apply to the Docker image, on the form:

            [<registry>/]<repository-name>[:<tag>]

        Where the `<repository-name>` may contain any number of separating slashes `/`, depending on
        the `default_repository` from configuration or the `repository` field on the target
        `docker_image`.

        This method will always return a non-empty tuple.
        """
        repository = self.format_repository(default_repository, interpolation_context)
        image_names = tuple(
            ":".join(s for s in [repository, self.format_tag(tag, interpolation_context)] if s)
            for tag in self.tags.value or ()
        )

        registries_options = tuple(registries.get(*(self.registries.value or [])))
        if not registries_options:
            # The image name is also valid as image ref without registry.
            return image_names

        return tuple(
            "/".join([registry.address, image_name])
            for image_name in image_names
            for registry in registries_options
        )


def get_build_options(
    context: DockerBuildContext,
    field_set: DockerFieldSet,
    global_target_stage_option: str | None,
    target: Target,
) -> Iterator[str]:
    # Build options from target fields inheriting from DockerBuildOptionFieldMixin
    for field_type in target.field_types:
        if issubclass(field_type, DockerBuildOptionFieldMixin):
            source = DockerInterpolationContext.TextSource(
                address=target.address, target_alias=target.alias, field_alias=field_type.alias
            )
            format = partial(
                context.interpolation_context.format,
                source=source,
                error_cls=DockerImageOptionValueError,
            )
            yield from target[field_type].options(format)

    # Target stage
    target_stage = None
    if global_target_stage_option in context.stages:
        target_stage = global_target_stage_option
    elif field_set.target_stage.value:
        target_stage = field_set.target_stage.value
        if target_stage not in context.stages:
            raise DockerBuildTargetStageError(
                f"The {field_set.target_stage.alias!r} field in `{target.alias}` "
                f"{field_set.address} was set to {target_stage!r}"
                + (
                    f", but there is no such stage in `{context.dockerfile}`. "
                    f"Available stages: {', '.join(context.stages)}."
                    if context.stages
                    else f", but there are no named stages in `{context.dockerfile}`."
                )
            )

    if target_stage:
        yield from ("--target", target_stage)


@rule
async def build_docker_image(
    field_set: DockerFieldSet,
    options: DockerOptions,
    global_options: GlobalOptions,
    docker: DockerBinary,
    process_cleanup: ProcessCleanupOption,
) -> BuiltPackage:
    context, wrapped_target = await MultiGet(
        Get(
            DockerBuildContext,
            DockerBuildContextRequest(
                address=field_set.address,
                build_upstream_images=True,
            ),
        ),
        Get(WrappedTarget, Address, field_set.address),
    )

    tags = field_set.image_refs(
        default_repository=options.default_repository,
        registries=options.registries(),
        interpolation_context=context.interpolation_context,
    )

    process = docker.build_image(
        build_args=context.build_args,
        digest=context.digest,
        dockerfile=context.dockerfile,
        env=context.build_env.environment,
        tags=tags,
        extra_args=tuple(
            get_build_options(
                context=context,
                field_set=field_set,
                global_target_stage_option=options.build_target_stage,
                target=wrapped_target.target,
            )
        ),
    )
    result = await Get(FallibleProcessResult, Process, process)

    if result.exit_code != 0:
        maybe_msg = docker_build_failed(
            field_set.address,
            context,
            global_options.options.colors,
        )
        if maybe_msg:
            logger.warning(maybe_msg)

        raise ProcessExecutionFailure(
            result.exit_code,
            result.stdout,
            result.stderr,
            process.description,
            process_cleanup=process_cleanup.val,
        )

    logger.debug(
        dedent(
            f"""\
            Docker build output for {tags[0]}:
            stdout:
            {result.stdout.decode()}

            stderr:
            {result.stderr.decode()}
            """
        )
    )

    return BuiltPackage(
        result.output_digest,
        (BuiltDockerImage.create(tags),),
    )


def docker_build_failed(address: Address, context: DockerBuildContext, colors: bool) -> str | None:
    if not context.copy_source_vs_context_source:
        return None

    msg = (
        f"Docker build failed for `docker_image` {address}. The {context.dockerfile} have `COPY` "
        "instructions where the source files may not have been found in the Docker build context."
        "\n\n"
    )

    renames = [
        format_rename_suggestion(src, dst, colors=colors)
        for src, dst in context.copy_source_vs_context_source
        if src and dst
    ]
    if renames:
        msg += (
            f"However there are possible matches. Please review the following list of suggested "
            f"renames:\n\n{bullet_list(renames)}\n\n"
        )

    unknown = [src for src, dst in context.copy_source_vs_context_source if not dst]
    if unknown:
        msg += (
            f"The following files were not found in the Docker build context:\n\n"
            f"{bullet_list(unknown)}\n\n"
        )

    unreferenced = [dst for src, dst in context.copy_source_vs_context_source if not src]
    if unreferenced:
        if len(unreferenced) > 10:
            unreferenced = unreferenced[:9] + [f"... and {len(unreferenced)-9} more"]
        msg += (
            f"There are additional files in the Docker build context that were not referenced by "
            f"any `COPY` instruction (this is not an error):\n\n{bullet_list(unreferenced)}\n\n"
        )

    return msg


def rules():
    return [
        *collect_rules(),
        UnionRule(PackageFieldSet, DockerFieldSet),
        UnionRule(RunFieldSet, DockerFieldSet),
    ]