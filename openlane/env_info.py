#!/usr/bin/env python3

# Copyright 2021-2023 Efabless Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Note: This file may be run with no dependencies installed as part of
# environment surveys. Please ensure all code as compatible as possible
# with ancient versions of Python.

import os
import re
import sys
import json
import platform
import subprocess

try:
    from typing import Optional  # noqa: F401
except ImportError:
    pass

CONTAINER_ENGINE = os.getenv("OPENLANE_CONTAINER_ENGINE", "docker")


class StringRepresentable(object):
    def __str__(self):
        return str(self.__dict__)

    def __repr__(self):
        return str(self.__dict__)


class ContainerInfo(StringRepresentable):
    engine = "UNKNOWN"  # type: str
    version = "UNKNOWN"  # type: str
    conmon = False  # type: bool
    rootless = False  # type : bool

    def __init__(self):
        self.engine = "UNKNOWN"
        self.version = "UNKNOWN"
        self.conmon = False
        self.rootless = False

    @staticmethod
    def get():
        # type: () -> Optional['ContainerInfo']
        try:
            cinfo = ContainerInfo()

            try:
                info_str = subprocess.check_output(
                    [CONTAINER_ENGINE, "info", "--format", "{{json .}}"]
                ).decode("utf8")
            except Exception:
                return None

            try:
                info = json.loads(info_str)
            except Exception:
                raise Exception("Result from 'docker info' was not valid JSON.")

            if info.get("host") is not None:
                if info["host"].get("conmon") is not None:
                    cinfo.conmon = True
                    if (
                        info["host"].get("remoteSocket") is not None
                        and "podman" in info["host"]["remoteSocket"]["path"]
                    ):
                        cinfo.engine = "podman"

                        cinfo.version = info["version"]["Version"]
            elif (
                info.get("Docker Root Dir") is not None
                or info.get("DockerRootDir") is not None
            ):
                cinfo.engine = "docker"

                # Get Version
                try:
                    version_output = (
                        subprocess.check_output([CONTAINER_ENGINE, "--version"])
                        .decode("utf8")
                        .strip()
                    )
                    cinfo.version = re.split(r"\s", version_output)[2].strip(",")
                except Exception:
                    print("Could not extract Docker version.", file=sys.stderr)

                security_options = info.get("SecurityOptions")
                for option in security_options:
                    if "rootless" in option:
                        cinfo.rootless = True

            return cinfo
        except Exception as e:
            print(e, file=sys.stderr)
            return None


class OSInfo(StringRepresentable):
    kernel = ""  # type: str
    python_version = ""  # type: str
    kernel_version = ""  # type: str
    distro = None  # type: Optional[str]
    distro_version = None  # type: Optional[str]
    container_info = None  # type: Optional[ContainerInfo]
    supported = False  # type: bool

    def __init__(self):
        self.kernel = platform.system()
        self.python_version = platform.python_version()
        self.kernel_version = (
            platform.release()
        )  # Unintuitively enough, it's the kernel's release
        self.distro = None
        self.distro_version = None
        self.container_info = None
        self.supported = self.kernel in ["Darwin", "Linux", "Windows"]

    @staticmethod
    def get():
        # type: () -> 'OSInfo'
        osinfo = OSInfo()

        if osinfo.kernel == "Windows":
            osinfo.distro = "Windows"
            osinfo.distro_version = platform.release()
            osinfo.kernel_version = platform.version()

        if osinfo.kernel == "Darwin":
            osinfo.distro = "macOS"
            osinfo.distro_version = platform.mac_ver()[0]
            osinfo.kernel_version = platform.release()

        if osinfo.kernel == "Linux":
            os_release = ""
            try:
                os_release += open("/etc/lsb-release").read()
            except FileNotFoundError:
                pass
            try:
                os_release += open("/etc/os-release").read()
            except FileNotFoundError:
                pass

            if os_release.strip() != "":
                config = {}
                for line in os_release.split("\n"):
                    if line.strip() == "":
                        continue
                    key, value = line.split("=")
                    value = value.strip('"')

                    config[key] = value

                osinfo.distro = config.get("ID") or config.get("DISTRIB_ID")
                osinfo.distro_version = config.get("VERSION_ID") or config.get(
                    "DISTRIB_RELEASE"
                )

            else:
                print("Failed to get distribution info.", file=sys.stderr)

        osinfo.container_info = ContainerInfo.get()

        return osinfo


if __name__ == "__main__":
    print(OSInfo.get())
