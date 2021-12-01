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


import collections
import pathlib as pl
import subprocess
import sys
from typing import Dict, List, Type


from agent_build.tools import constants
from agent_build import package_builders
from tests.package_tests.internals import docker_test
from agent_build.tools.environment_deployments import deployments

_PARENT_DIR = pl.Path(__file__).parent
__SOURCE_ROOT__ = _PARENT_DIR.parent.parent.absolute()

# The global collection of all test. It is used by CI aimed scripts in order to be able to perform those test just
# by knowing the name of needed test.
ALL_PACKAGE_TESTS: Dict[str, 'Test'] = {}

# Maps package test of some package to the builder of this package. Also needed for the GitHub Actions CI to
# create a job matrix for a particular package tests.
PACKAGE_BUILDER_TESTS: Dict[package_builders.PackageBuilder, List['Test']] = collections.defaultdict(list)


class Test:
    """
    Particular package test. If combines information about the package type, architecture,
    deployment and the system where test has to run.
    """
    def __init__(
            self,
            base_name: str,
            package_builder: package_builders.PackageBuilder,
            additional_deployment_steps: List[Type[deployments.DeploymentStep]],
            deployment_architecture: constants.Architecture = None,
    ):
        """
        :param base_name: Base name of the test.
        :param package_builder: Builder instance to build the image.
        :param additional_deployment_steps: Additional deployment steps that may be needed to perform the test.
            They are additionally performed after the deployment steps of the package builder.
        :param deployment_architecture: Architecture of the machine where the test's deployment has to be perform.
            by default it is an architecture of the package builder.
        """
        self._base_name = base_name
        self.package_builder = package_builder
        self.architecture = deployment_architecture or package_builder.architecture

        # since there may be needed to build the package itself first, we have to also deploy the steps
        # from the package builder's deployment, to provide needed environment for the builder,
        # so we add the steps from the builder's deployment first.
        additional_deployment_steps = [
            *[type(step) for step in package_builder.deployment.steps],
            *additional_deployment_steps
        ]

        self.deployment = deployments.Deployment(
            name=self.unique_name,
            step_classes=additional_deployment_steps,
            architecture=deployment_architecture or package_builder.architecture,
            base_docker_image=package_builder.base_docker_image
        )

        if self.unique_name in ALL_PACKAGE_TESTS:
            raise ValueError(f"The package test with name: {self.unique_name} already exists.")

        # Add the current test to the global tests collection so it can be invoked from command line.
        ALL_PACKAGE_TESTS[self.unique_name] = self
        # Also add it to the package builders tests collection.
        PACKAGE_BUILDER_TESTS[self.package_builder].append(self)

    @property
    def unique_name(self) -> str:
        """
        The unique name of the package test. It contains information about all specifics that the test has.
        """
        return f"{self.package_builder.name}_{self._base_name}".replace("-", "_")


class DockerImagePackageTest(Test):
    """
    Test for the agent docker images.
    """
    def __init__(
            self,
            target_image_architecture: constants.Architecture,
            base_name: str,
            package_builder: package_builders.ContainerPackageBuilder,
            additional_deployment_steps: List[Type[deployments.DeploymentStep]],
            deployment_architecture: constants.Architecture = None,
    ):
        """
        :param target_image_architecture: Architecture in which to perform the image test.
        :param base_name: Base name of the test.
        :param package_builder: Builder instance to build the image.
        :param additional_deployment_steps: Additional deployment steps that may be needed to perform the test.
            They are additionally performed after the deployment steps of the package builder.
        :param deployment_architecture: Architecture of the machine where the test's deployment has to be perform.
            by default it is an architecture of the package builder.
        """

        self.target_image_architecture = target_image_architecture

        super().__init__(
            base_name,
            package_builder,
            additional_deployment_steps,
            deployment_architecture=deployment_architecture
        )

        # Do the trick to help to static analyser.
        self.package_builder: package_builders.ContainerPackageBuilder = package_builder

    @property
    def unique_name(self) -> str:
        return f"{self._base_name}_{self.target_image_architecture.value}"

    def run_test(
            self,
            scalyr_api_key: str,
    ):
        """
        Run test for the agent docker image.
        First of all it builds an image, then pushes it to the local registry and does full test.

        :param scalyr_api_key:  Scalyr API key.
        """

        registry_host = "localhost:5000"

        registry_container_name = "agent_images_registry"

        # Spin up local docker registry where to push the result image

        # first of all delete existing registry container.
        subprocess.check_call([
            "docker", "rm", "-f", registry_container_name
        ])

        # Run container with docker registry.
        subprocess.check_call([
            "docker",
            "run",
            "--rm",
            "-d",
            "-p",
            "5000:5000",
            "--name",
            registry_container_name,
            "registry:2"
        ])

        try:
            # Build image and push it to the local registry.
            # Instead of calling the build function run the build_package script,
            # so it can also be tested.
            subprocess.check_call(
                [
                    sys.executable,
                    "build_package_new.py",
                    self.package_builder.name,
                    "--registry",
                    registry_host,
                    "--tag",
                    "latest",
                    "--tag",
                    "test",
                    "--tag",
                    "debug",
                    "--push"
                ],
                cwd=str(__SOURCE_ROOT__)
            )

            # Test that all tags has been pushed to the registry.
            for tag in ["latest", "test", "debug"]:
                full_image_name = f"{registry_host}/{self.package_builder.RESULT_IMAGE_NAME}:{tag}"

                # Remove the local image first, if exists.
                subprocess.check_call([
                    "docker", "image", "rm", "-f", full_image_name
                ])

                # Login to the local registry.
                subprocess.check_call([
                    "docker",
                    "login",
                    "--password",
                    "nopass",
                    "--username",
                    "nouser",
                    registry_host
                ])

                # Pull the image
                try:
                    subprocess.check_call([
                        "docker", "pull", full_image_name
                    ])
                except subprocess.CalledProcessError as e:
                    raise AssertionError("Can not pull the result image from local registry. "
                                         f"Error: {e}")

                # Remove the image once more.
                subprocess.check_call([
                    "docker", "image", "rm", "-f", full_image_name
                ])

            local_registry_image_name = f"{registry_host}/{self.package_builder.RESULT_IMAGE_NAME}"

            # Start the test
            docker_test.run(
                image_name=local_registry_image_name,
                scalyr_api_key=scalyr_api_key
            )
        finally:
            # Cleanup.
            # Removing registry container.
            subprocess.check_call([
                "docker", "rm", "-f", registry_container_name
            ])
            subprocess.check_call([
                "docker", "logout", registry_host
            ])

            subprocess.check_call([
                "docker", "image", "prune", "-f"
            ])

    @staticmethod
    def create_tests(
            package_builder: package_builders.ContainerPackageBuilder,
            additional_deployment_steps: List[Type[deployments.DeploymentStep]] = None,
            target_image_architectures: List[constants.Architecture] = None,
    ) -> List['DockerImagePackageTest']:
        """
        The helper function that allows to create multiple tests for the agent docker image.
            The number of result tests depends on how many architectures are specified and each test will test
            test the particular architecture of the image.

        :param package_builder: Builder instance that has to build the image.
        :param additional_deployment_steps: Additional deployment steps that may be needed to perform the test.
            They are additionally performed after the deployment steps of the package builder.
        :param target_image_architectures: List of architectures in which to perform the image tests.
        :return: List of tesult test instances. The order is the same as in the 'target_image_architectures' argument.
        """
        result_tests = []
        target_image_architectures = target_image_architectures or []
        additional_deployment_steps = additional_deployment_steps or []

        for target_arch in target_image_architectures:

            test_instance = DockerImagePackageTest(
                base_name=package_builder.PACKAGE_TYPE.value,
                target_image_architecture=target_arch,
                package_builder=package_builder,

                # since there may be needed to build the package itself first, we have to also deploy the steps
                # from the package builder's deployment, to provide needed environment for the builder,
                # so we add the steps from the builder's deployment first.
                additional_deployment_steps=[
                    *[type(step) for step in package_builder.deployment.steps],
                    *additional_deployment_steps
                ]
            )

            result_tests.append(test_instance)

        return result_tests


# Create tests for the all docker images (json/syslog/ api) and for k8s image.
DockerImagePackageTest.create_tests(
    # Specify the builder that has to build the image.
    package_builder=package_builders.DOCKER_JSON_CONTAINER_BUILDER,

    # Specify which architectures of the result image has to be tested.
    target_image_architectures=[
        constants.Architecture.X86_64,
        constants.Architecture.ARM64,
    ]
)

DockerImagePackageTest.create_tests(
    package_builder=package_builders.DOCKER_SYSLOG_CONTAINER_BUILDER,
    target_image_architectures=[
        constants.Architecture.X86_64,
        constants.Architecture.ARM64,
    ]
)

DockerImagePackageTest.create_tests(
    package_builder=package_builders.DOCKER_API_CONTAINER_BUILDER,
    target_image_architectures=[
        constants.Architecture.X86_64,
        constants.Architecture.ARM64,
    ]
)

DockerImagePackageTest.create_tests(
    package_builder=package_builders.K8S_CONTAINER_BUILDER,
    target_image_architectures=[
        constants.Architecture.X86_64,
        constants.Architecture.ARM64,
    ]
)



