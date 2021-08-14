# Copyright 2014 Scalyr Inc.
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
# ------------------------------------------------------------------------
#
# author: Steven Czerwinski <czerwin@scalyr.com>

from __future__ import absolute_import

__author__ = "czerwin@scalyr.com"


import platform
import os
import sys
import pathlib as pl
import enum
from typing import Tuple


PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3
PY2_pre_279 = PY2 and sys.version_info < (2, 7, 9)
PY3_pre_32 = PY3 and sys.version_info < (3, 2)


# One of the main things this file does is correctly give the full path to the following directories regardless of
# install type :
#   install_root:  The top-level directory where Scalyr is currently installed.
#
# There are several different ways we can be running, and this files has to give the correct values for each of them.
# For example, we could just be running out of the source tree as checked into github.  Or we can be running
# out of some package such as Deb, Rpm, Msi for Windows and etc. We handle of these cases.
#
# Example layouts for different install types:
#
# Running from source:
#   Here the install root is ~/scalyr-agent-2 and the package root is ~/scalyr-agent-2/scalyr-agent
#
# Install using tarball:
#   Here the install root is the path to the extracted tarball.
#
# Install using rpm/deb/kubernetes/docker package:
#   Here the install root is /usr/share/scalyr-agent-2.
#
# Install using msi package for Windows:
#   Here the install root is C:\Program Files (x86)\Scalyr\
#
# In spite of the package type, the internal structure of the package is common:
#   VERSION: The package version file.
#   install_type: File which contains the type of the installed package. Agent reads this file during the start in order
#       to find all essential paths on the system where it runs. Soo the 'InstallType' enum class to see its possible
#       values.
#   py/: Directory with the source code of the agent. Only used in the Kubernetes and Docker images, other build use
#       frozen binaries.
#   bin/: The directory with executables. For the majority of the package types, it contains frozen binaries of the
#       agent. In case of Kubernetes or Docker it contains symlinks to agent executable scripts in the source code which
#       is located in the 'py' folder.
#   certs/: Certificates directory.
#   monitors/: The folder for custom monitors.
#   misc/: Miscellaneous files.


# Indicates if this code was compiled into a single executable via PyInstaller.  If that's the case,
# then we cannot rely on __file__ and the source is kind of through into the same directory.
__is_frozen__ = hasattr(sys, "frozen")


class PlatformType(enum.Enum):
    """
    The Enum class with possible types of the OS. Firstly, used for the 'P'
    """

    WINDOWS = "windows"
    LINUX = "linux"
    POSIX = "posix"


def __determine_platform():
    system_name = platform.system().lower()
    if system_name.startswith("win"):
        return PlatformType.WINDOWS
    elif system_name.startswith("linux"):
        return PlatformType.LINUX
    else:
        return PlatformType.POSIX


PLATFORM_TYPE = __determine_platform()


# The enum  for INSTALL_TYPE, a variable declared down below.
class InstallType(enum.Enum):
    """
    The enumeration of the Scalyr agent installation types. It is used for INSTALL_TYPE, a variable declared down below.
    """

    # Those package types contain Scalyr Agent as frozen binary.
    PACKAGE_INSTALL = "package"  # Indicates it was installed via a package manager such as RPM or Windows executable.
    TARBALL_INSTALL = "packageless"  # Indicates it was installed via a tarball.

    # This type runs Scalyr Agent from the source code.
    DEV_INSTALL = "dev"  # Indicates source code is running out of the original source tree, usually during dev testing.


def __read_install_type_from_type_file(path: pl.Path) -> InstallType:
    if not path.is_file():
        raise FileNotFoundError(
            f"Can not determine the package installation type. The file '{path}' is not found."
        )
    # Read the type of the package from the file.
    install_type = path.read_text().strip()
    # Check if the package type is one of the valid install types.
    if install_type not in [e.value for e in InstallType]:
        raise ValueError(
            f"Can not determine the installation type. Unknown value: {install_type}"
        )

    return InstallType(install_type)


def __determine_install_root_and_type() -> Tuple[str, InstallType]:
    """
    Determine the path for the install root and type of the installation.
    """
    if __is_frozen__:
        # All installation types that use frozen binary of the Scalyr agent follow the same file structure,
        # so it's just needed to specify the relative path to the install root from the current executable binary path.

        # The executable frozen binary should be in the <install-root>/bin folder.
        # Since it is a frozen binary, then the 'sys.executable' has to work as a path for the frozen binary itself,
        # so we can find the 'bin' folder from it.
        bin_dir = pl.Path(sys.executable).parent

        # Get the install root - the parent directory of the 'bin' folder.
        install_root = bin_dir.parent

        # All agent packages have the special file 'install_type' which contains the type of the package.
        # This file is always located in the install root, so it is a good way to verify if it is a install root or not.
        install_type_file_path = install_root / "install_type"

        return str(install_root), __read_install_type_from_type_file(
            install_type_file_path
        )

    else:
        # The agent code is not frozen. The main task here is determine whether the agent has been started from the
        # source code (aka DEV_INSTALL) or from the package installation. In packages, the source code is located in the
        # '<install_root>/py' folder, so it has to be enough to verify if the parent folder of the 'scalyr_agent'
        # package is a folder named 'py'.

        script_path = pl.Path(sys.argv[0])

        # First of all get the real path of the executed script if it is a symlink.
        while script_path.is_symlink():
            script_path = pl.Path(script_path.parent, os.readlink(script_path)).resolve()

        # parent folder of the script has to be a 'scalyr_agent' package folder.
        package_folder = script_path.parent

        # Get the source code root. The name of the folder has to be 'py'
        source_code_root = package_folder.parent

        if source_code_root.name == "py":
            install_root = source_code_root.parent
            install_type_file_path = install_root / "install_type"

            return str(install_root), __read_install_type_from_type_file(install_type_file_path)
        else:
            # The name of hte parent folder of the 'scalyr_agent' package is not 'py', so it is likely that it
            # started from source code.
            return str(source_code_root), InstallType.DEV_INSTALL


__install_root__, INSTALL_TYPE = __determine_install_root_and_type()


def get_install_root():
    """
    The root of the agent installation.
    """
    return __install_root__


def __determine_version():
    """
    Returns the agent version number, read from the VERSION file."""

    version_file_path = pl.Path(get_install_root()) / "VERSION"
    return version_file_path.read_text().strip()


SCALYR_VERSION = __determine_version()
