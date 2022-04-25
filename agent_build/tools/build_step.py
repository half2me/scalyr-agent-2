# Copyright 2014-2021 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import abc
import argparse
import collections
import dataclasses
import enum
import functools
import hashlib
import getpass
import json
import os
import pathlib as pl
import shlex
import re
import shutil
import stat
import subprocess
import logging
import sys
import tempfile
from typing import Union, Optional, List, Dict, Type, ClassVar

from agent_build.tools import common
from agent_build.tools import constants


_PARENT_DIR = pl.Path(__file__).parent.parent.absolute()
_AGENT_BUILD_PATH = constants.SOURCE_ROOT / "agent_build"
_DEPLOYMENT_STEPS_PATH = (
    _AGENT_BUILD_PATH / "tools" / "environment_deployments" / "steps"
)


_REL_AGENT_BUILD_PATH = pl.Path("agent_build")
_REL_AGENT_REQUIREMENT_FILES_PATH = _REL_AGENT_BUILD_PATH / "requirement-files"


def calculate_files_checksum(
    files: List[pl.Path]
) -> str:
    """
    Calculate and return sha256 checksum of all files from a specified list.
    """
    files = sorted(files)
    sha256 = hashlib.sha256()
    for file_path in sorted(files):
        sha256.update(str(file_path).encode())
        abs_path = constants.SOURCE_ROOT / file_path
        sha256.update(abs_path.read_bytes())

    return sha256.hexdigest()



class DeploymentStepError(Exception):
    """
    Special exception class for the step error.
    """


@dataclasses.dataclass
class StepCICDSettings:
    cacheable: bool = dataclasses.field(default=False)
    prebuilt_in_separate_job: bool = dataclasses.field(default=False)


class BuildStep:
    """
    Base abstraction that represents set of action that has to be performed in order to prepare some environment,
        for example for the build. The deployment step can be performed directly on the current machine or inside the
    docker. Results of the DeploymentStep can be cached. The caching is mostly aimed to reduce build time on the
        CI/CD such as Github Actions.
    """
    TRACKED_FILE_GLOBS = []
    NO_CI_CD_CACHE = False
    NAME: str = None
    CACHEABLE: bool = False

    def __init__(
        self,
        name: str = None,
        dependency_steps: List['BuildStep'] = None,
        additional_settings: Dict[str, str] = None,
        ci_cd_settings: StepCICDSettings = None,
        global_steps_collection: List['BuildStep'] = None
    ):
        """
        :param deployment: The deployment instance where this step is added.
        :param architecture: Architecture of the machine where step has to be performed.
        :param previous_step: If None then step is considered as first and it doesn't have to be performed on top
            of another step. If this is an instance of another DeploymentStep, then this step will be performed on top
            it. It also can be a string with some docker image. In this case the step has to be performed in that docker
            image, and the step is also considered as a first step(without previous steps).
        """

        self.name = name or type(self).NAME

        # List of steps which results are required for the current step.
        self._dependency_steps = dependency_steps or []

        # Collection of NAME-VALUE pairs to pass to the script.
        self._additional_settings = additional_settings or {}

        # # The root path where the step has to operate and create/locate needed files.
        # self._build_root = build_root

        # Dict with all information about a step. All things whose change may affect work of this step, has to be
        # reflected here.
        self._overall_info: Optional[Dict] = None
        # List of paths of files which are used by this step.
        # Step calculates a checksum of those files in order to generate its unique id.
        self._tracked_file_paths = None
        self._base_step: Optional[BuildStep] = None

        self._ci_cd_settings = ci_cd_settings or StepCICDSettings()

        # Directory path where this step (and maybe its nested steps) will store its result.
        # Initialized only during the run of the step.
        self._build_root: Optional[pl.Path] = None

        if global_steps_collection is not None and self._ci_cd_settings.cacheable:
            global_steps_collection.append(self)


    @property
    def output_directory(self) -> pl.Path:
        return self._build_root / "step_outputs" / self.id

    @property
    def _temp_output_directory(self) -> pl.Path:
        return self.output_directory.parent / f"~{self.output_directory.name}"

    @property
    def tracked_file_paths(self):
        """
        Create a final list of all files that has to be included in the step's checksum calculation.
        """

        if self._tracked_file_paths:
            return self._tracked_file_paths

        found_paths = set()

        # Resolve file globs to get all files to track.
        for file_glob in self._tracked_file_globs:

            if file_glob.is_absolute():
                file_glob = file_glob.relative_to(constants.SOURCE_ROOT)
            glob_paths = set(constants.SOURCE_ROOT.glob(str(file_glob)))
            found_paths = found_paths.union(glob_paths)

        # To exclude all untracked files we use values from the .dockerignore file.
        dockerignore_path = constants.SOURCE_ROOT / ".dockerignore"
        dockerignore_content = dockerignore_path.read_text()

        paths_excluded = []
        for line in dockerignore_content.splitlines():
            if not line:
                continue

            glob = pl.Path(line)

            # If pattern on .dockerignore  starts with '/', remove it.
            if glob.is_absolute():
                glob = glob.relative_to("/")

            # Iterate though all found paths and remove everything that matches values from .dockerignore.
            for f in found_paths:
                if not f.match(str(glob)):
                    continue

                paths_excluded.append(f)

        # Iterate through excluded paths and also exclude child paths for directories.
        for ex_path in list(paths_excluded):
            if not ex_path.is_dir():
                continue
            children_to_exclude = list(ex_path.glob("**/*"))
            paths_excluded.extend(children_to_exclude)

        # Remove excluded paths.
        filtered_paths = list(found_paths - set(paths_excluded))

        # Remove directories.
        filtered_paths = list(filter(lambda p: not p.is_dir(), filtered_paths))

        filtered_paths.append(constants.SOURCE_ROOT / ".dockerignore")
        filtered_paths = [
            p.relative_to(constants.SOURCE_ROOT) for p in filtered_paths
        ]
        self._tracked_file_paths = sorted(list(filtered_paths))
        return self._tracked_file_paths

    def _init_overall_info(self):
        """
        Create overall info dictionary by collecting any information that can affect caching of that step.
        In other words, if step results has been cached by using one set of data and that data has been changed later,
        then the old cache does not reflect that changes and has to be invalidated.
        """
        self._overall_info = {
            "name": self.name,
            # List of all files that are used by step.
            "used_files": [str(p) for p in self.tracked_file_paths],
            # Checksum of the content of that files, to catch any change in that files.
            "files_checksum": calculate_files_checksum(self.tracked_file_paths),
            # Similar overall info's but from steps that are required by the current step.
            # If something changes in that dependency steps, then this step will also reflect that change.
            "dependency_steps": [s.overall_info for s in self._dependency_steps],
            # Same overall info but for the base step.
            "base_step": self._base_step.overall_info if self._base_step else None,
            # Add additional setting of the step.
            "additional_settings": self._additional_settings,
        }

    @property
    def overall_info(self) -> Dict:
        """
        Returns dictionary with all information that is sensitive for the caching of that step.
        """
        if not self._overall_info:
            self._init_overall_info()

        return self._overall_info

    @property
    def overall_info_str(self) -> str:
        return json.dumps(
            self.overall_info,
            sort_keys=True,
            indent=4
        )

    @property
    def id(self) -> str:
        """
        Unique identifier of the step.
        It is based on the checksum of the step's :py:attr:`overall_info` attribute.
        Steps overall_info has to reflect any change in step's input data, so that also has to
        be reflected in its id.
        """

        sha256 = hashlib.sha256()

        sha256.update(self.overall_info_str.encode())

        checksum = sha256.hexdigest()

        name = f"{self.name}__{checksum}".lower()

        # # Also reflect in the id that the step in not cacheable, so CI/CD can skip it.
        # if not self._ci_cd_settings.cacheable:
        #     name = f"{name}_skip_cache"

        return name

    @property
    def all_used_cacheable_steps(self) -> List['BuildStep']:
        """
        Return list that includes all steps (including nested and the current one) that are used in that final step and
        are supposed to be cached in CI/CD.
        """
        result_steps = []
        # Add all dependency steps:
        for ds in self._dependency_steps:
            result_steps.extend(ds.all_used_cacheable_steps)

        # Add base step if presented.
        if self._base_step:
            result_steps.extend(self._base_step.all_used_cacheable_steps)

        # Add this step itself, but only if it cacheable.
        if self._ci_cd_settings.cacheable:
            result_steps.append(self)

        return result_steps

    @property
    def all_used_cached_step_ids(self) -> List[str]:
        """
        Return ids of this step and ids of all steps that are used by it.
        This function is needed to use that ids in CI/CD and pre-fetch some of the cached results.
        """

        return [step.id for step in self.all_used_cacheable_steps]

    def _check_for_cached_result(self):
        return self.output_directory.exists()

    def run(self, build_root: pl.Path):
        """
        Run the step. Based on its initial data, it will be performed in docker or locally, on the current system.
        :param additional_input: Additional input to the step as that can be passed to constructor, but since this
            input is specified after the initialization of the step, it can not be cached.
        """

        self._build_root = build_root.absolute()

        if self._check_for_cached_result():
            logging.info(
                f"The cache of the deployment step {self.id} is found, reuse it and skip it."
            )
        else:

            # Run all dependency steps first.
            for step in self._dependency_steps:
                step.run(build_root=build_root)

            # Then also run the base step.
            if self._base_step:
                self._base_step.run(build_root=build_root)

            # Create a temporary directory for the output of the current step.
            if self._temp_output_directory.is_dir():
                shutil.rmtree(self._temp_output_directory)

            self._temp_output_directory.mkdir(parents=True)

            # Write step's info to a file in its output, for easier troubleshooting.
            info_file_path = self._temp_output_directory / "step_info.txt"
            info_file_path.write_text(self.overall_info_str)

            self._run()

            if common.IN_CICD and type(self).NO_CI_CD_CACHE:
                # If we are in Ci/CD and this step is marked to not save its result in cache, then
                # put a special file in the root of the step's cache folder, so CI/CD can find this file and skip this
                # cache.
                skip_cache_file = self._temp_output_directory / "skip_cache_to_cicd"
                skip_cache_file.touch()

            # Rename temp output directory to a final.
            self._temp_output_directory.rename(self.output_directory)

    @abc.abstractmethod
    def _run(self):
        pass

    @property
    def _tracked_file_globs(self) -> List[pl.Path]:
        globs = type(self).TRACKED_FILE_GLOBS[:]
        return globs


class SimpleBuildStep(BuildStep):
    """
    Base abstraction that represents set of action that has to be performed in order to prepare some environment,
        for example for the build. The deployment step can be performed directly on the current machine or inside the
    docker. Results of the DeploymentStep can be cached. The caching is mostly aimed to reduce build time on the
        CI/CD such as Github Actions.
    """

    def __init__(
        self,
        name: str,
        base_step: Union['BuildStep', str] = None,
        dependency_steps: List['BuildStep'] = None,
        additional_settings: Dict[str, str] = None,
        ci_cd_settings: StepCICDSettings = None,
        global_steps_collection: List['BuildStep'] = None
    ):
        super(SimpleBuildStep, self).__init__(
            name=name,
            dependency_steps=dependency_steps,
            additional_settings=additional_settings,
            ci_cd_settings=ci_cd_settings,
            global_steps_collection=global_steps_collection
        )

        self._base_step = base_step

    @abc.abstractmethod
    def _run(self):
        pass


@dataclasses.dataclass
class DockerImageSpec:
    name: str
    architecture: constants.Architecture

    def as_dict(self):
        return {
            "name": self.name,
            "architecture": self.architecture.value
        }

    def load_image(self):
        """
        Load docker image from tar file.
        """
        output = (
            common.check_output_with_log(
                ["docker", "images", "-q", self.name]
            ).decode().strip()
        )
        if output:
            return

        common.run_command(["docker", "load", "-i", str(self.name)])

    def save_image(self, output_path: pl.Path):
        """
        Serialize docker image into file by using 'docker save' command.
        :param output_path: Result output file.
        """
        with output_path.open("wb") as f:
            common.check_call_with_log(["docker", "save", self.name], stdout=f)


class ScriptBuildStep(BuildStep):
    """
    Base abstraction that represents set of action that has to be performed in order to prepare some environment,
        for example for the build. The deployment step can be performed directly on the current machine or inside the
    docker. Results of the DeploymentStep can be cached. The caching is mostly aimed to reduce build time on the
        CI/CD such as Github Actions.
    """

    def __init__(
        self,
        name: str,
        script_path: pl.Path,
        is_dependency_step: bool,
        base_step: Union['BuildStep', "ScriptBuildStep", DockerImageSpec] = None,
        dependency_steps: List['ScriptBuildStep'] = None,
        additional_settings: Dict[str, str] = None,
        ci_cd_settings: StepCICDSettings = None,
        global_steps_collection: List['BuildStep'] = None
    ):
        """
        :param deployment: The deployment instance where this step is added.
        :param architecture: Architecture of the machine where step has to be performed.
        :param previous_step: If None then step is considered as first and it doesn't have to be performed on top
            of another step. If this is an instance of another DeploymentStep, then this step will be performed on top
            it. It also can be a string with some docker image. In this case the step has to be performed in that docker
            image, and the step is also considered as a first step(without previous steps).
        """

        super(ScriptBuildStep, self).__init__(
            name=name,
            dependency_steps=dependency_steps,
            additional_settings=additional_settings,
            ci_cd_settings=ci_cd_settings,
            global_steps_collection=global_steps_collection
        )

        self._script_path = script_path

        # That flag indicates that the step does not produce any artifact, instead, it
        # makes changes changes to its current environment and this environment will be the base for the
        # next step.
        self.is_dependency_step = is_dependency_step

        if base_step is None:
            # If there's no a base step, then this step starts from scratch on the current system.
            self._base_step = None
            self.base_docker_image = None
        else:
            if isinstance(base_step, DockerImageSpec):
                # If the base step is docker spec, then the step start from scratch too, but
                # inside docker image.
                self._base_step = None
                self.base_docker_image = base_step
            else:
                # In other case it has to be another step and the current step has to be perform on top of it.
                self._base_step = base_step

                # Also use result docker image of the base step as base docker image if presented.
                if isinstance(base_step, ScriptBuildStep):
                    self.base_docker_image = base_step.result_image
                else:
                    self.base_docker_image = None

    @property
    def _source_root(self):
        return self._build_root / "step_isolated_source_roots" / self.id

    def _init_overall_info(self):
        """
        Also add the information about docker image to the overall info.
        """
        super(ScriptBuildStep, self)._init_overall_info()
        if self.base_docker_image:
            self._overall_info["docker_image"] = self.base_docker_image.as_dict()

    @property
    def result_image(self) -> Optional[DockerImageSpec]:
        """
        The name of the result docker image, just the same as cache key.
        """
        if self.runs_in_docker:
            return DockerImageSpec(
                name=self.id,
                architecture=self.base_docker_image.architecture
            )
        else:
            return None

    @property
    def runs_in_docker(self) -> bool:
        """
        Whether this step has to be performed in docker or not.
        """
        return self.base_docker_image is not None

    def _save_step_docker_container_as_image_if_needed(
            self,
            container_name: str
    ):
        """
        Save container with the result of the step execution as docker image.
        :param container_name: Name of the container to save.
        """

        # If this is a dependency step, then we don't need to save it's image.
        if self.is_dependency_step:
            return

        common.run_command([
            "docker", "commit", container_name, self.result_image.name
        ])

        image_file_path = self._temp_output_directory / f"{self.id}.tar"

        self.result_image.save_image(
            output_path=image_file_path
        )

    @property
    def result_image_path(self):
        return self._temp_output_directory / f"{self.id}.tar"

    def _run(self):
        self._prepare_working_source_root()

        try:
            if self.runs_in_docker and not common.IN_DOCKER:
                self._run_in_docker()
            else:
                logging.info("555555")
                self._run_locally()
        except Exception:
            globs = [str(g) for g in self._tracked_file_globs]
            logging.error(
                f"'{type(self).__name__}' has failed. "
                "HINT: Make sure that you have specified all files. "
                f"For now, tracked files are: {globs}"
            )
            raise DeploymentStepError(f"Step has failed. Step name: '{self.id}'.")

    @property
    def _in_docker_dependency_outputs_path(self):
        return pl.Path("/tmp/step/dependencies")

    @property
    def _tracked_file_globs(self) -> List[pl.Path]:
        globs = super(ScriptBuildStep, self)._tracked_file_globs
        globs.append(self._script_path)
        return globs

    def _get_command_line_args(self) -> List[str]:
        """
        Create list with the shell command line arguments that has to execute the shell script.
            Optionally also adds cache path to the shell script as additional argument.
        :return: String with shell command that can be executed to needed shell.
        """

        required_steps_outputs = []

        if self.runs_in_docker:
            root_dir = self._in_docker_source_root_path
        else:
            root_dir = self._source_root

        for req_step in self._dependency_steps:
            if self.runs_in_docker:
                req_step_output = self._in_docker_dependency_outputs_path / req_step.output_directory.name
            else:
                req_step_output = req_step.output_directory

            required_steps_outputs.append(str(req_step_output))

        rel_script_path = self._script_path.relative_to(constants.SOURCE_ROOT)

        # Determine needed shell interpreter.
        if rel_script_path.suffix == ".ps1":
            full_command_args = [
                "powershell",
            ]
        elif rel_script_path.suffix == ".sh":
            full_command_args = [
                "/bin/bash",
            ]
        elif rel_script_path.suffix == ".py":
            full_command_args = [
                "python3"
            ]

        full_command_args.extend([
            str(rel_script_path),
            *required_steps_outputs
        ])

        return full_command_args

    @property
    def _in_docker_source_root_path(self):
        return pl.Path(f"/tmp/agent_source")

    def _run_locally(self):
        """
        Run step locally by running the script on current system.
        """

        command_args = self._get_command_line_args()

        # Copy current environment.
        env = os.environ.copy()

        env["STEP_OUTPUT_PATH"] = str(self._temp_output_directory)

        # Also set all additional settings as environment variables.
        for name, value in self._additional_settings.items():
            if value is None:
                continue
            env[name] = value

        env["SOURCE_ROOT"] = str(self._source_root)

        if common.IN_CICD:
            env["IN_CICD"] = "1"


        common.check_call_with_log(
            command_args,
            env=env,
            cwd=str(self._source_root),
        )

    def _prepare_working_source_root(self):
        """
        Prepare directory with source root of the project which is
        isolated directory with only files that are tracked by the step.
        """

        #self._source_root = self.step_root / "source"

        # os.chmod(constants.DEPLOYMENT_ISOLATED_ROOTS_DIR, constants.DEPLOYMENT_ISOLATED_ROOTS_DIR.stat().st_mode | stat.S_IEXEC)
        # for p in constants.DEPLOYMENT_ISOLATED_ROOTS_DIR.glob("**/*"):
        #     os.chown(p, os.getuid(), os.getgid())

        # # Create new isolated source root and copy only tracked files there.
        # self._source_root = constants.DEPLOYMENT_ISOLATED_ROOTS_DIR / self.id

        if self._source_root.is_dir():
            common.check_call_with_log(f"ls -al {self._source_root}/..", shell=True)
            shutil.rmtree(self._source_root)

        self._source_root.mkdir(parents=True)

        # Copy all tracked files to new isolated directory.
        for file_path in self.tracked_file_paths:
            source_path = constants.SOURCE_ROOT / file_path
            dest_path = self._source_root / file_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, dest_path)

        a=10

    def _check_for_cached_result(self):
        exists = super(ScriptBuildStep, self)._check_for_cached_result()

        # If step runs in docker, it's not a dependency step and its result image is already
        # been found in cache, then load that existing image.
        if exists and self.runs_in_docker and not self.is_dependency_step:
            self.result_image.load_image()

        return exists

    def _run_in_docker(
            self
    ):
        """
        Run step in docker. It uses a special logic, which is implemented in 'agent_build/tools/tools.build_in_docker'
        module,that allows to execute custom command inside docker by using 'docker build' command. It differs from
        running just in the container because we can benefit from the docker caching mechanism.
        :param locally: If we are already in docker, this has to be set as True, to avoid loop.
        """

        container_name = "agent-build-deployment-step"

        cmd_args = self._get_command_line_args()

        common.run_command([
            "docker", "rm", "-f", container_name
        ])

        # in_docker_deployment_cache_dir = constants.DEPLOYMENT_OUTPUTS_DIR.relative_to(constants.SOURCE_ROOT)
        # in_docker_deployment_cache_dir = self._in_docker_source_root_path / in_docker_deployment_cache_dir

        #output_rel_path = self._temp_output_directory.relative_to(constants.SOURCE_ROOT)
        in_docker_output_path = "/tmp/step/output"

        env_variables_options = []

        # Set additional settings ass environment variables.
        for name, value in self._additional_settings.items():
            env_variables_options.append("-e")
            env_variables_options.append(f"{name}={value}")

        env_variables_options.extend([
            "-e",
            f"STEP_OUTPUT_PATH={in_docker_output_path}",
            "-e",
            f"SOURCE_ROOT={self._in_docker_source_root_path}",
            "-e",
            "AGENT_BUILD_IN_DOCKER=1"
        ])

        if self._base_step:
            base_image = self._base_step.result_image
        else:
            base_image = self.base_docker_image

        volumes_mapping = [
            "-v",
            f"{self._source_root}:{self._in_docker_source_root_path}",
            "-v",
            f"{self._temp_output_directory}:{in_docker_output_path}",
        ]

        for dependency_step in self._dependency_steps:
            in_docker_dependency_output = self._in_docker_dependency_outputs_path / dependency_step.output_directory.name
            volumes_mapping.extend([
                "-v",
                f"{dependency_step.output_directory}:{in_docker_dependency_output}"
            ])

        try:
            common.check_call_with_log([
                "docker",
                "run",
                "-i",
                "--name",
                container_name,
                *volumes_mapping,
                "--platform",
                base_image.architecture.as_docker_platform,
                # "-v",
                # f"{constants.DEPLOYMENT_OUTPUTS_DIR}:{in_docker_deployment_cache_dir}",
                # "--mount",
                # f"type=bind,source={self._source_root},target={self._in_docker_source_root_path}",
                # "--mount",
                # f"type=bind,source={constants.DEPLOYMENT_CACHES_DIR},target={in_docker_deployment_cache_dir}",
                *env_variables_options,
                "--workdir",
                str(self._in_docker_source_root_path),
                base_image.name,
                *cmd_args
            ])

            #constants.DEPLOYMENT_OUTPUTS_DIR.chmod(constants.DEPLOYMENT_OUTPUTS_DIR.stat().st_mode | stat.S_IEXEC)

            self._save_step_docker_container_as_image_if_needed(
                container_name=container_name
            )
        finally:
            common.run_command([
                "docker", "rm", "-f", container_name
            ])


# class PrepareEnvironmentStep(ScriptBuildStep):
#     """
#     """
#     def __init__(
#         self,
#         script_path: pl.Path,
#         build_root: pl.Path,
#         base_step: Union['PrepareEnvironmentStep', str] = None,
#         dependency_steps: List['ScriptBuildStep'] = None,
#         additional_settings: Dict[str, str] = None,
#         ci_cd_settings: StepCICDSettings = None
#     ):
#
#         super(PrepareEnvironmentStep, self).__init__(
#             script_path=script_path,
#             build_root=build_root,
#             is_dependency_step=True,
#             base_step=base_step,
#             dependency_steps=dependency_steps,
#             additional_settings=additional_settings,
#             ci_cd_settings=ci_cd_settings
#         )
#
#
# class ArtifactStep(ScriptBuildStep):
#     def __init__(
#         self,
#         script_path: pl.Path,
#         build_root: pl.Path,
#         base_step: Union['PrepareEnvironmentStep', str] = None,
#         dependency_steps: List['ScriptBuildStep'] = None,
#         additional_settings: Dict[str, str] = None,
#         ci_cd_settings: StepCICDSettings = None
#     ):
#
#         super(ArtifactStep, self).__init__(
#             script_path=script_path,
#             build_root=build_root,
#             is_dependency_step=False,
#             base_step=base_step,
#             dependency_steps=dependency_steps,
#             additional_settings=additional_settings,
#         )


class Deployment:
    """
    Abstraction which represents some final desired state of the environment which is defined by set of steps, which are
    instances of the :py:class:`DeploymentStep`
    """

    def __init__(
        self,
        name: str,
        step_classes: List[Type[ScriptBuildStep]],
        architecture: constants.Architecture = constants.Architecture.UNKNOWN,
        base_docker_image: str = None,
        inputs: Dict = None
    ):
        """
        :param name: Name of the deployment. Must be unique for the whole project.
        :param step_classes: List of step classes. All those steps classes will be instantiated
            by using current specifics.
        :param architecture: Architecture of the machine where deployment and its steps has to be performed.
        :param base_docker_image: Name of the docker image, if the deployment and all its steps has to be performed
            inside that docker image.
        """
        self.name = name
        self.architecture = architecture
        self.base_docker_image = base_docker_image
        self.cache_directory = constants.DEPLOYMENT_CACHES_DIR / self.name
        self.output_directory = constants.DEPLOYMENT_OUTPUT / self.name

        # List with instantiated steps.
        self.steps = collections.OrderedDict()

        for name, step_cls in step_classes.items():
            step = step_cls.create(
                architecture=architecture,
                step_inputs=inputs
                # specify previous step for the current step.
            )
            self.steps[name] = step

        # Add this instance to the global collection of all deployments.
        if self.name in ALL_DEPLOYMENTS:
            raise ValueError(f"The deployment with name: {self.name} already exists.")

        ALL_DEPLOYMENTS[self.name] = self

    def get_step_by_alias(self, alias):
        return self.steps[alias]

    @property
    def output_path(self) -> pl.Path:
        """
        Path to the directory where the deployment's steps can put their results.
        """
        return constants.DEPLOYMENT_OUTPUT / self.name

    @property
    def in_docker(self) -> bool:
        """
        Flag that shows whether this deployment has to be performed in docker or not.
        """
        # If the base image is defined, then this deployment is meant to be
        # performed in docker.

        return self.base_docker_image is not None

    @property
    def result_image_name(self) -> Optional[str]:
        """
        The name of the result image of the whole deployment if it has to be performed in docker. It's, logically,
        just a result image name of the last step.
        """
        return self.steps[-1].result_image.lower()

    def deploy(self):
        """
        Perform the deployment by running all deployment steps.
        """

        for step in self.steps.values():
            step.run()


# Special collection where all created deployments are stored. All of the  deployments are saved with unique name as
# key, so it is possible to find any deployment by its name. The ability to find needed deployment step by its name is
# crucial if we want to run it on the CI/CD.
ALL_DEPLOYMENTS: Dict[str, "Deployment"] = {}


def get_deployment_by_name(name: str) -> Deployment:
    return ALL_DEPLOYMENTS[name]


# Step that runs small script which installs requirements for the test/dev environment.
class InstallTestRequirementsDeploymentStep(ScriptBuildStep):
    @property
    def script_path(self) -> pl.Path:
        return _DEPLOYMENT_STEPS_PATH / "deploy-test-environment.sh"

    @property
    def _tracked_file_globs(self) -> List[pl.Path]:
        globs = super(InstallTestRequirementsDeploymentStep, self)._tracked_file_globs
        globs.append(_REL_AGENT_REQUIREMENT_FILES_PATH / "*.txt")
        return globs


_REL_DOCKER_BASE_IMAGE_STEP_PATH = _DEPLOYMENT_STEPS_PATH / "docker-base-images"
_REL_AGENT_BUILD_DOCKER_PATH = _REL_AGENT_BUILD_PATH / "docker"


_REL_DEPLOYMENT_BUILD_BASE_IMAGE_STEP = (
        _DEPLOYMENT_STEPS_PATH / "build_base_docker_image"
)


class BuildDockerBaseImageStep(ScriptBuildStep):
    """
    This deployment step is responsible for the building of the base image of the agent docker images.
    It runs shell script that builds that base image and pushes it to the local registry that runs in container.
    After push, registry is shut down, but it's data root is preserved. This step puts this
    registry data root to the output of the deployment, so the builder of the final agent docker image can access this
    output and fetch base images from registry root (it needs to start another registry and mount existing registry
    root).
    """

    # Suffix of that python image, that is used as the base image for our base image.
    # has to match one of the names from the 'agent_build/tools/environment_deployments/steps/build_base_docker_image'
    # directory, except 'build_base_images_common_lib.sh', it is a helper library.
    BASE_DOCKER_IMAGE_TAG_SUFFIX: str

    @property
    def script_path(self) -> pl.Path:
        """
        Resolve path to the base image builder script which depends on suffix on the base image.
        """
        return (
            _REL_DEPLOYMENT_BUILD_BASE_IMAGE_STEP
            / f"{type(self).BASE_DOCKER_IMAGE_TAG_SUFFIX}.sh"
        )

    @property
    def _tracked_file_globs(self) -> List[pl.Path]:
        globs = super(BuildDockerBaseImageStep, self)._tracked_file_globs
        # Track the dockerfile...
        globs.append(_REL_AGENT_BUILD_DOCKER_PATH / "Dockerfile.base")
        # and helper lib for base image builder.
        globs.append(
            _REL_DEPLOYMENT_BUILD_BASE_IMAGE_STEP / "build_base_images_common_lib.sh"
        )
        # .. and requirement files...
        globs.append(
            _REL_AGENT_REQUIREMENT_FILES_PATH / "docker-image-requirements.txt"
        )
        globs.append(_REL_AGENT_REQUIREMENT_FILES_PATH / "compression-requirements.txt")
        globs.append(_REL_AGENT_REQUIREMENT_FILES_PATH / "main-requirements.txt")
        return globs



class BuildBusterDockerBaseImageStep(BuildDockerBaseImageStep):
    """
    Subclass that builds agent's base docker image based on debian buster (slim)
    """

    BASE_DOCKER_IMAGE_TAG_SUFFIX = "slim"


class BuildAlpineDockerBaseImageStep(BuildDockerBaseImageStep):
    """
    Subclass that builds agent's base docker image based on alpine.
    """

    BASE_DOCKER_IMAGE_TAG_SUFFIX = "alpine"

#
# # Create common test environment that will be used by GitHub Actions CI
# COMMON_TEST_ENVIRONMENT = Deployment(
#     # Name of the deployment.
#     # Call the local './.github/actions/perform-deployment' action with this name.
#     "test_environment",
#     step_classes=[InstallTestRequirementsDeploymentStep],
# )





















#########


class PrepareBaseCentos(ScriptBuildStep):
    SCRIPT_PATH = _DEPLOYMENT_STEPS_PATH / "prepare_base_centos.sh"


class PrepareBaseUbuntu(ScriptBuildStep):
    SCRIPT_PATH = _DEPLOYMENT_STEPS_PATH / "prepare_base_ubuntu.sh"


# class BuildPythonCentosStep(ShellScriptDeploymentStep):
#     BASE_STEP = PrepareBaseCentos
#     IS_ARTIFACT_STEP = True
#     SCRIPT_PATH = _REL_DEPLOYMENT_STEPS_PATH / "build_python.sh"
#
#     def __init__(
#             self,
#             base_os: str,
#             docker_image: DeploymentStep.DockerImageSpec = None,
#
#     ):
#
#         if base_os == "centos:6":
#             base_step_class = PrepareBaseCentos
#         elif base_os == "ubuntu:20.04":
#             base_step_class = PrepareBaseUbuntu
#         else:
#             raise ValueError(f"Wrong base os name: {base_os}.")
#
#         prepare_base = base_step_class(
#             docker_image=docker_image
#         )
#         super(BuildPythonCentosStep, self).__init__(
#             base_step=prepare_base,
#             docker_image=docker_image
#         )

class InstallBuildDependenciesStepDistroType(enum.Enum):
    CENTOS = "centos"
    UBUNTU = "ubuntu"


class InstallBuildDependenciesStep(ScriptBuildStep):

    def __init__(
            self,
            distro_type: InstallBuildDependenciesStepDistroType,
            docker_image: DockerImageSpec = None,
    ):

        if distro_type == InstallBuildDependenciesStepDistroType.CENTOS:
            script_name = "prepare_base_centos.sh"
        elif distro_type == InstallBuildDependenciesStepDistroType.UBUNTU:
            script_name = "prepare_base_ubuntu.sh"

        super(InstallBuildDependenciesStep, self).__init__(
            script_path=_DEPLOYMENT_STEPS_PATH / script_name,
            base_step=docker_image
        )


class BuildPythonStep(ScriptBuildStep):

    is_dependency_step = True

    def __init__(
            self,
            # distro_type: InstallBuildDependenciesStepDistroType,
            # docker_image: str = None
            initial_distro_step: ScriptBuildStep

    ):

        # install_builder_dependencies_step = InstallBuildDependenciesStep(
        #     distro_type=distro_type,
        #     docker_image=docker_image,
        # )

        super(BuildPythonStep, self).__init__(
            script_path=_DEPLOYMENT_STEPS_PATH / "build_python.sh",
            base_step=initial_distro_step
        )


class PreparePythonBase(ScriptBuildStep):
    def __init__(
            self,
            # distro_type: InstallBuildDependenciesStepDistroType,
            # docker_image: DeploymentStep.DockerImageSpec = None
            initial_distro_step: ScriptBuildStep
    ):

        # install_build_dependencies_step = InstallBuildDependenciesStep(
        #     distro_type=distro_type,
        #     docker_image=docker_image
        # )

        build_python_step = BuildPythonStep(
            initial_distro_step=initial_distro_step
        )

        super(PreparePythonBase, self).__init__(
            script_path=_DEPLOYMENT_STEPS_PATH / "prepare_python_base.sh",
            base_step=initial_distro_step,
            dependency_steps=[build_python_step]
        )


class PrepareAgentPythonDependencies(ScriptBuildStep):
    #BASE_STEP = PrepareBaseCentos
    TRACKED_FILE_GLOBS = [_REL_AGENT_REQUIREMENT_FILES_PATH / "*.txt"]
    is_dependency_step = True

    def __init__(
            self,
            initial_distro_step: ScriptBuildStep
    ):

        base_python_step = PreparePythonBase(
            initial_distro_step=initial_distro_step
        )

        super(PrepareAgentPythonDependencies, self).__init__(
            script_path=_DEPLOYMENT_STEPS_PATH / "prepare_agent_python-dependencies.sh",
            base_step=base_python_step,
        )

    @property
    def _tracked_file_globs(self) -> List[pl.Path]:
        return super(PrepareAgentPythonDependencies, self)._tracked_file_globs


class PrepareFrozenBinaryBuilderStep(ScriptBuildStep):

    def __init__(
        self,
        # distro_type: InstallBuildDependenciesStepDistroType,
        # docker_image: DeploymentStep.DockerImageSpec = None
        initial_distro_step: ScriptBuildStep
    ):

        base_python_step = PreparePythonBase(
            initial_distro_step=initial_distro_step
        )

        prepare_agent_deps_step = PrepareAgentPythonDependencies(
            initial_distro_step=initial_distro_step
        )

        super(PrepareFrozenBinaryBuilderStep, self).__init__(
            script_path=_DEPLOYMENT_STEPS_PATH / "prepare_frozen_binary_builder.sh",
            base_step=base_python_step,
            dependency_steps=[prepare_agent_deps_step],
        )


class BuildPythonUbuntuStep(ScriptBuildStep):
    BASE_STEP = PrepareBaseUbuntu
    is_dependency_step = True
    SCRIPT_PATH = _DEPLOYMENT_STEPS_PATH / "build_python.sh"


class PrepareFpmBuilderStep(ScriptBuildStep):
    #SCRIPT_PATH = _REL_DEPLOYMENT_STEPS_PATH / "prepare_fpm_builder.sh"

    def __init__(
        self,
        # distro_type: InstallBuildDependenciesStepDistroType,
        # docker_image: str = None
        initial_distro_step: ScriptBuildStep
    ):

        base_python_step = PreparePythonBase(
            initial_distro_step=initial_distro_step
        )

        super(PrepareFpmBuilderStep, self).__init__(
            script_path=_DEPLOYMENT_STEPS_PATH / "prepare_fpm_builder.sh",
            base_step=base_python_step,
        )


@dataclasses.dataclass
class FrozenBinaryBuildSpec:
    distro_type: InstallBuildDependenciesStepDistroType
    docker_image: DockerImageSpec = None


class BuildFrozenBinaryStep(ScriptBuildStep):
    is_dependency_step = True
    TRACKED_FILE_GLOBS = [
        pl.Path("agent_build/**/*"),
        pl.Path("scalyr_agent/**/*"),
        pl.Path("config/**/*"),
        pl.Path("docker/**/*"),
        pl.Path("monitors/**/*"),
        pl.Path("certs/**/*"),
        pl.Path("VERSION"),
        pl.Path("CHANGELOG.md"),
        pl.Path("build_package_new.py"),
    ]

    def __init__(
            self,
            frozen_binary_build_spec: FrozenBinaryBuildSpec,
            package_install_type: str
    ):

        centos_base_step = InstallBuildDependenciesStep(
            distro_type=frozen_binary_build_spec.distro_type,
            docker_image=frozen_binary_build_spec.docker_image
        )

        frozen_binaries_base_step = PrepareFrozenBinaryBuilderStep(
            initial_distro_step=centos_base_step
        )

        super(BuildFrozenBinaryStep, self).__init__(
            script_path=constants.SOURCE_ROOT / "agent_build/tools/environment_deployments/steps/build_frozen_binary.py",
            base_step=frozen_binaries_base_step,
        )

        self._add_input(
            "INSTALL_TYPE", package_install_type
        )

        # self._add_input(
        #     "FROZEN_BINARY_FILE_NAME", frozen_binary_build_spec.frozen_binary_file_name
        # )


class BuildFpmPackageStep(ScriptBuildStep):
    """
    Base image builder for packages which are produced by the 'fpm' packager.
    For example dep, rpm.
    """
    INSTALL_TYPE = "package"

    def __init__(
            self,
            architecture: constants.Architecture,
            result_package_file_name: str,
            package_architecture: str,
            fpm_package_type: str,
            package_install_type: str,
            variant: str = None,
            no_versioned_file_name: bool = False,
            frozen_binary_builder_spec: FrozenBinaryBuildSpec = None,
    ):

        self.architecture = architecture
        self._variant = variant
        self._no_versioned_file_name = no_versioned_file_name
        self._result_package_filename = result_package_file_name
        self._package_architecture = package_architecture
        self._fpm_package_type = fpm_package_type

        required_steps = []

        if frozen_binary_builder_spec:
            build_frozen_binary_step = BuildFrozenBinaryStep(
                frozen_binary_build_spec=frozen_binary_builder_spec,
                package_install_type=package_install_type
            )

            required_steps.append(build_frozen_binary_step)

        ubuntu_base_step = InstallBuildDependenciesStep(
            distro_type=InstallBuildDependenciesStepDistroType.UBUNTU,
            docker_image=ScriptBuildStep.DockerImageSpec(
                name="ubuntu:20.04",
                architecture=constants.Architecture.X86_64
            )
        )

        prepare_python_step = PreparePythonBase(
            initial_distro_step=ubuntu_base_step
        )

        super(BuildFpmPackageStep, self).__init__(
            script_path=pl.Path(constants.SOURCE_ROOT) / "agent_build/tools/environment_deployments/steps/build_fpm_package.py",
            base_step=prepare_python_step,
            dependency_steps=required_steps
        )

        self._add_input(
            "FPM_PACKAGE_TYPE", fpm_package_type
        )
        self._add_input(
            "VARIANT", variant
        )
        self._add_input(
            "NO_VERSIONED_FILE_NAME", str(int(no_versioned_file_name))
        )
        self._add_input(
            "PACKAGE_ARCHITECTURE", package_architecture
        )
        self._add_input(
            "RESULT_PACKAGE_FILE_NAME", result_package_file_name,
        )


class FinalStep:
    def __init__(
            self,
            used_steps: List[BuildStep]
    ):
        self._used_steps = used_steps or []

    @property
    def all_used_cacheable_steps(self) -> List[BuildStep]:
        result_steps = []
        for s in self._used_steps:
            result_steps.extend(s.all_used_cacheable_steps)

        return result_steps

    @property
    def all_used_cacheable_steps_ids(self) -> List[str]:
        return [s.id for s in self.all_used_cacheable_steps]

    @abc.abstractmethod
    def _run(self):
        pass

    def run(self, build_root: pl.Path):
        for s in self._used_steps:
            s.run(build_root=build_root)

        self._run()

