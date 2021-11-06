#!/usr/bin/env python3

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

import pathlib
import pathlib as pl
import subprocess
import json
import time
import os
import tarfile
import logging
import sys

from agent_build import package_builders
from tests.package_tests.common import LogVerifier, AgentLogRequestStatsLineCheck, AssertAgentLogLineIsNotAnErrorCheck


USER_HOME = pl.Path("~").expanduser()

# Flag that indicates that this test script is executed as frozen binary.
__frozen__ = hasattr(sys, "frozen")


def install_deb_package(package_path: pl.Path):
    env = os.environ.copy()
    if __frozen__:
        env[
            "LD_LIBRARY_PATH"
        ] = f'/lib/x86_64-linux-gnu:{os.environ["LD_LIBRARY_PATH"]}'

    subprocess.check_call(
        ["dpkg", "-i", str(package_path)],
        env=env
    )


def install_rpm_package(package_path: pl.Path):
    env = os.environ.copy()

    if __frozen__:
        env["LD_LIBRARY_PATH"] = "/libx64"

    subprocess.check_call(
        ["rpm", "-i", str(package_path)],
        env=env
    )


def install_tarball(package_path: pl.Path):
    tar = tarfile.open(package_path)
    tar.extractall(USER_HOME)
    tar.close()


def _get_tarball_install_path() -> pl.Path:
    matched_paths = list(USER_HOME.glob("scalyr-agent-*.*.*"))
    if len(matched_paths) == 1:
        return pl.Path(matched_paths[0])

    raise ValueError("Can't find extracted tar file.")


def install_msi_package(package_path: pl.Path):
    subprocess.check_call(f"msiexec.exe /I {package_path} /quiet", shell=True)


def install_package(
        package_type: package_builders.PackageType,
        package_path: pl.Path
):
    if package_type == package_builders.PackageType.DEB:
        install_deb_package(package_path)
    elif package_type == package_builders.PackageType.RPM:
        install_rpm_package(package_path)
    elif package_type == package_builders.PackageType.TAR:
        install_tarball(package_path)
    elif package_type == package_builders.PackageType.MSI:
        install_msi_package(package_path)


def _get_msi_install_path() -> pl.Path:
    return pl.Path(os.environ["programfiles(x86)"]) / "Scalyr"


def start_agent(package_type: package_builders.PackageType):
    if package_type in [
        package_builders.PackageType.DEB,
        package_builders.PackageType.RPM,
        package_builders.PackageType.TAR
    ]:

        if package_type == package_builders.PackageType.MSI:
            # Add agent binaries to the PATH env. variable on windows.
            bin_path = _get_msi_install_path() / "bin"
            os.environ["PATH"] = f"{bin_path};{os.environ['PATH']}"

        subprocess.check_call(f"scalyr-agent-2 start", shell=True, env=os.environ)
    elif package_type == package_builders.PackageType.TAR:
        tarball_dir = _get_tarball_install_path()

        binary_path = tarball_dir / "bin/scalyr-agent-2"
        subprocess.check_call([binary_path, "start"])


def get_agent_status(package_type: package_builders.PackageType):
    if package_type in [
        package_builders.PackageType.DEB,
        package_builders.PackageType.RPM,
        package_builders.PackageType.TAR
    ]:
        subprocess.check_call(f"scalyr-agent-2 status -v", shell=True)
    elif package_type == package_builders.PackageType.TAR:
        tarball_dir = _get_tarball_install_path()

        binary_path = tarball_dir / "bin/scalyr-agent-2"
        subprocess.check_call([binary_path, "status", "-v"])


def stop_agent(package_type: package_builders.PackageType):
    if package_type in [
        package_builders.PackageType.DEB,
        package_builders.PackageType.RPM,
        package_builders.PackageType.TAR
    ]:
        subprocess.check_call(f"scalyr-agent-2 stop", shell=True)
    if package_type == package_builders.PackageType.TAR:
        tarball_dir = _get_tarball_install_path()

        binary_path = tarball_dir / "bin/scalyr-agent-2"
        subprocess.check_call([binary_path, "stop"])


def configure_agent(package_type: package_builders.PackageType, api_key: str):
    if package_type in[
        package_builders.PackageType.DEB,
        package_builders.PackageType.RPM,
    ]:
        config_path = pathlib.Path(AGENT_CONFIG_PATH)
    elif package_type == package_builders.PackageType.TAR:
        install_path = _get_tarball_install_path()
        config_path = install_path / "config/agent.json"
    elif package_type == package_builders.PackageType.MSI:
        config_path = _get_msi_install_path() / "config" / "agent.json"

    config = {}
    config["api_key"] = api_key

    config["server_attributes"] = {"serverHost": "ARTHUR_TEST"}

    # TODO enable and test system and process monitors
    config["implicit_metric_monitor"] = False
    config["implicit_agent_process_metrics_monitor"] = False
    config["verify_server_certificate"] = False
    config_path.write_text(json.dumps(config))


def remove_deb_package():
    env = os.environ.copy()

    if __frozen__:
        env[
            "LD_LIBRARY_PATH"
        ] = f'/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu:{os.environ["LD_LIBRARY_PATH"]}'
    subprocess.check_call(
        f"apt-get remove -y scalyr-agent-2", shell=True,
        env=env
    )


def remove_rpm_package():
    env = os.environ.copy()

    if __frozen__:
        env["LD_LIBRARY_PATH"] = "/libx64"

    subprocess.check_call(
        f"yum remove -y scalyr-agent-2", shell=True,
        env=env
    )


def remove_package(package_type: package_builders.PackageType):
    if package_type == package_builders.PackageType.DEB:
        remove_deb_package()
    elif package_type == package_builders.PackageType.RPM:
        remove_rpm_package()


AGENT_CONFIG_PATH = "/etc/scalyr-agent-2/agent.json"


def _get_logs_path(package_type: package_builders.PackageType) -> pl.Path:
    if package_type in [
        package_builders.PackageType.DEB,
        package_builders.PackageType.RPM
    ]:
        return pl.Path("/var/log/scalyr-agent-2")
    elif package_type == package_builders.PackageType.TAR:
        return _get_tarball_install_path() / "log"
    elif package_type == package_builders.PackageType.MSI:
        return _get_msi_install_path() / "log"


def run(
        package_path: pl.Path,
        package_type: package_builders.PackageType,
        scalyr_api_key: str,
):
    if not package_path.exists():
        logging.error("No package.")
        exit(1)

    install_package(package_type, package_path)

    configure_agent(package_type, scalyr_api_key)

    start_agent(package_type)

    time.sleep(2)

    agent_log_path = _get_logs_path(package_type) / "agent.log"

    with agent_log_path.open("rb") as f:
        logging.info("Start verifying the agent.log file.")
        agent_log_verifier = LogVerifier()
        agent_log_verifier.set_new_content_getter(f.read)

        # Add check for any ERROR messages to the verifier.
        agent_log_verifier.add_line_check(AssertAgentLogLineIsNotAnErrorCheck())
        # Add check for the request stats message.
        agent_log_verifier.add_line_check(AgentLogRequestStatsLineCheck(), required_to_pass=True)

        # Start agent.log file verification.
        agent_log_verifier.verify(timeout=300)

        logging.info("Agent.log:")
        logging.info(agent_log_path.read_text())

    get_agent_status(package_type)

    time.sleep(2)

    stop_agent(package_type)

    remove_package(package_type)