# Copyright 2014-2020 Scalyr Inc.
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

"""
Module responsible for parsing data from build_info file which is available with each agent package.

Keep in mind that that file is only available for package and not for dev installs.
"""

from __future__ import unicode_literals
from __future__ import absolute_import

if False:  # NOSONAR
    from typing import Dict

import six

from scalyr_agent import __scalyr__
from scalyr_agent.compat import subprocess_check_output

GIT_GET_HEAD_REVISION_CMD = "git rev-parse HEAD"


def get_build_info():
    # type: () -> Dict[str, str]
    """Get build info dict from install info."""

    return __scalyr__.__install_info__.get("build_info", {})


# def get_build_info_str():
#
#     build_info_str = """Packaged by: {}
#     Latest commit: {}
#     From branch: {}
#     Build time: {}
#     """.format(
#         build_info_str["packaged_by"],
#         build_info_str["latest_commit"],
#         build_info_str["from_branch"],
#         build_info_str["build_time"],
#     )


def get_build_revision_from_git():
    # type: () -> str
    """
    Return build revision from git ref log (if available).

    NOTE: This function is only used on dev (non-package) installs.
    """
    cmd = GIT_GET_HEAD_REVISION_CMD

    try:
        output = subprocess_check_output(cmd, shell=True)
    except Exception:
        return "unknown"

    return six.ensure_text(output).strip()


def get_build_revision():
    # type: () -> str
    """
    This function retrieves git commit which was used to build the agent package from the
    build_info file which is generated by build_package.py script.

    If we are running on a dev install, it retrieves the commit revision by querying git reflog
    instead.
    """

    build_info = get_build_info()

    # If there's no build_info, try to get revision from the current git.
    if not build_info:
        return get_build_revision_from_git()

    return build_info.get("latest_commit", "unknown")
