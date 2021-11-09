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

$ProgressPreference = "SilentlyContinue"
$ErrorActionPreference = "Stop"

$cache_path=$args[0]

if ($args[0]) {
    $cache_path=$args[0]
} else {
    $cache_path = "$Env:TEMP\$([System.IO.Path]::GetRandomFileName())"
}

New-Item -ItemType Directory -Force -Path "$cache_path"

# TODO: We use pre-installed Python version in the github actions, but it provides only python3.7 in its windows runners,
# so it would be great to install python3.8. The commented code works fine on fresh systems, but we have change it in
# order to be able to install Python3.8 on Github Actions runner.

# $python_installer_path = "$cache_path\python-3.8.10-amd64.exe"
#
# if (!(Test-Path $python_installer_path -PathType Leaf)) {
#     echo "Download python installer."
#     wget -O "$python_installer_path" https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe
# }
#
# $python_path = "C:\Python3"
# Start-Process "$python_installer_path" -ArgumentList "/quiet /passive TargetDir=$python_path" -wait

$wix_installer_path = "$cache_path\wix311-binaries.zip"
if (!(Test-Path $wix_installer_path -PathType Leaf)) {
    echo "Download WIX toolset."
    wget https://github.com/wixtoolset/wix3/releases/download/wix3112rtm/wix311-binaries.zip -OutFile "$wix_installer_path"
}

$wix_path = "C:\wix311"
Expand-Archive -LiteralPath "$wix_installer_path" -DestinationPath "$wix_path"

# Uncomment if there is no git on your machine. For now this is commented because there is a preinstalled git in the
# Github Actions.

# $git_installer_path = "$cache_path/git_install.exe"
# if (!(Test-Path $git_installer_path -PathType Leaf)) {
#     echo "Download git installer."
#     wget -O "$git_installer_path" https://github.com/git-for-windows/git/releases/download/v2.32.0.windows.1/Git-2.32.0-64-bit.exe
# }
# $git_path = "C:\Git"
# Start-Process "$git_installer_path" -ArgumentList "/VERYSILENT /DIR=$git_path" -wait


$old_path = (Get-ItemProperty -Path 'Registry::HKEY_CURRENT_USER\Environment' -Name path).path

$paths = "$wix_path"
$new_path = "$old_path;$paths"

Set-ItemProperty -Path 'Registry::HKEY_CURRENT_USER\Environment' -Name path -Value $new_path

$Env:Path = "$Env:Path;$paths"

# $script_path = $PSScriptRoot
# $source_root = (get-item $script_path ).parent.parent.FullName
#
# $pip_cache_path = "$cache_path\pip"
# if (Test-Path $pip_cache_path -PathType Container) {
#     cp $pip_cache_path (python -m pip cache dir)
#     Copy-Item -Path "$pip_cache_path\*" -Destination "$(python -m pip cache dir)" -Recurse
# }
#
# python -m pip install -r "$source_root\dev-requirements.txt"
#
# if (!(Test-Path $pip_cache_path -PathType Container)) {
#     Copy-Item -Path "$(python -m pip cache dir)" -Destination "$pip_cache_path" -Recurse
# }

Add-Content "$cache_path\paths.txt" "$wix_path" -Encoding utf8







