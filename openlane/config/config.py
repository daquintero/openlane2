# Copyright 2023 Efabless Corporation
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
import os
import json
import yaml
from glob import glob
from decimal import Decimal
from textwrap import dedent
from dataclasses import dataclass
from typing import (
    Any,
    ClassVar,
    Tuple,
    Union,
    List,
    Optional,
    Sequence,
    Callable,
    Dict,
)

from .variable import Variable
from .tcleval import env_from_tcl
from .resolve import resolve, Keys as SpecialKeys
from .flow import removed_variables, all_variables as flow_common_variables
from .pdk import (
    all_variables as pdk_variables,
    removed_variables as pdk_removed_variables,
    migrate_old_config,
)

from ..state import Path
from ..logging import info, warn
from ..common import GenericDict, GenericImmutableDict


@dataclass
class Meta:
    version: int = 1
    flow: Union[None, str, List[str]] = "Classic"


class InvalidConfig(ValueError):
    """
    An error raised when a configuration under resolution is invalid.

    :param config: A human-readable name for the particular configuration file
        causing this exception, i.e. whether it's a PDK configuration file or a
        user configuration file.
    :param warnings: A list of warnings generated during the loading of this
        configuration file.
    :param errors: A list of errors generated during the loading of this
        configuration file.
    :param args: Further arguments to be passed onto the constructor of
        :class:`ValueError`.
    :param message: An optional override for the Exception message.
    :param kwargs: Further keyword arguments to be passed onto the constructor of
        :class:`ValueError`.
    """

    def __init__(
        self,
        config: str,
        warnings: List[str],
        errors: List[str],
        message: Optional[str] = None,
        *args,
        **kwargs,
    ) -> None:
        self.config = config
        self.warnings = warnings
        self.errors = errors
        if message is None:
            message = "The following errors were encountered: \n"
            for error in self.errors:
                message += f"\t* {error}"
        super().__init__(message, *args, **kwargs)


class Config(GenericImmutableDict[str, Any]):
    """
    A map from OpenLane configuration variable keys to their values.

    It is recommended that you use :meth:`load` to create new, validated
    configurations from dictionaries or files.
    """

    current_interactive: ClassVar[Optional["Config"]] = None
    meta: Meta
    _interactive: bool = False

    def __init__(self, *args, meta: Optional[Meta] = None, **kwargs):
        super().__init__(*args, **kwargs)

        if meta is None:
            meta = Meta(version=1)

        self.meta = meta

    def is_interactive(self) -> bool:
        """
        :returns: Whether the configuration is for interactive mode or not.

        See :meth:`interactive` for more info on interactive mode.
        """
        return self._interactive

    def copy(self, **overrides) -> "Config":
        """
        Produces a shallow copy of the configuration object.

        :param overrides: A series of configuration overrides as key-value pairs.
            These values are NOT validated and you should not be overriding these
            haphazardly.
        """
        return Config(self, overrides=overrides)

    def to_raw_dict(self) -> Dict[str, Any]:
        final: Dict[Any, Any] = self._data.copy()
        final["meta"] = self.meta
        return final

    def _repr_markdown_(self) -> str:
        title = (
            "Interactive Configuration" if self.is_interactive() else "Configuration"
        )
        values_title = "Initial Values" if self.is_interactive() else "Values"
        return (
            dedent(
                f"""
                ### {title}
                #### {values_title}

                <br />

                ```yml
                %s
                ```
                """
            )
            % yaml.safe_dump(json.loads(self.dumps()))
        )

    @classmethod
    def get_meta(
        Self,
        json_config_in: Union[str, os.PathLike],
        flow_override: Optional[str] = None,
    ) -> Optional[Meta]:
        """
        Returns the Meta object of a JSON configuration file

        :param config_in: A configuration file.
        :returns: Either a Meta object, or if the file is not a JSON file, None.
        """
        try:
            obj = json.load(open(json_config_in, encoding="utf8"))
        except (json.JSONDecodeError, IsADirectoryError):
            return None

        meta = Meta()
        if meta_raw := obj.get("meta"):
            meta = Meta(**meta_raw)

        if flow_override is not None:
            meta.flow = flow_override

        return meta

    @classmethod
    def interactive(
        Self,
        DESIGN_NAME: str,
        PDK: str,
        STD_CELL_LIBRARY: Optional[str] = None,
        PDK_ROOT: Optional[str] = None,
        **kwargs,
    ) -> "Config":
        """
        This constructs a partial configuration object that may be incrementally
        adjusted per-step, and activates OpenLane's **interactive mode**.

        The interactive mode is overall less rigid than the pure mode, adding various
        references to global objects to make the REPL or Notebook experience more
        pleasant, however, it is not as resilient as the pure mode and should not
        be used in production code.

        :param DESIGN_NAME: The name of the design to be used.
        :param PDK: The name of the PDK.
        :param STD_CELL_LIBRARY: The name of the standard cell library.

            If not specified, the PDK's default SCL will be used.
        :param PDK_ROOT: Required if Volare is not installed.

            If Volare is installed, this value can be used to optionally override
            Volare's default.

        :param kwargs: Any overrides to PDK values and/or common flow default variables
            can be passed as keyword arguments to this function.

            Useful examples are CLOCK_PORT, CLOCK_PERIOD, et cetera, which while
            not bound to a specific :class:`Step`, affects most Steps' behavior.
        """
        PDK_ROOT = Self._resolve_pdk_root(PDK_ROOT)

        config_in, _, _ = Self._get_pdk_config(
            PDK,
            STD_CELL_LIBRARY,
            PDK_ROOT,
        )

        kwargs["DESIGN_NAME"] = DESIGN_NAME

        config_in = Config(config_in, overrides=kwargs)

        config_in, design_warnings, design_errors = config_in.process_variable_list(
            pdk_variables + list(flow_common_variables),
            removed_variables,
        )

        if len(design_errors) != 0:
            raise InvalidConfig("default configuration", design_warnings, design_errors)

        if len(design_warnings) > 0:
            info(
                "Loading the default configuration has generated the following warnings:"
            )
        for warning in design_warnings:
            warn(warning)

        config_in._interactive = True
        Config.current_interactive = config_in

        return config_in

    @classmethod
    def load(
        Self,
        config_in: Union[str, os.PathLike, Dict[str, Any]],
        flow_config_vars: Sequence[Variable],
        config_override_strings: Optional[Sequence[str]] = None,
        pdk: Optional[str] = None,
        pdk_root: Optional[str] = None,
        scl: Optional[str] = None,
        design_dir: Optional[str] = None,
    ) -> Tuple["Config", str]:
        """
        Returns a new Config object based on a Tcl file, a JSON file, or a
        dictionary.

        The returned config object is locked and cannot be modified.

        :param config_in: Either a file path to a JSON file or a Python
            dictionary representing an unprocessed OpenLane configuration
            object.

            Tcl files are also supported, but are deprecated and will be removed
            in the future.

        :param config_override_strings: A list of "overrides" in the form of
            NAME=VALUE strings. These are primarily for running OpenLane from
            the command-line and strictly speaking should not be used in the API.

        :param design_dir: The design directory for said configuration.
            Supported and required *if and only if* config_in is a dictionary.

        :param pdk: A process design kit to use. Required unless specified via the
            "PDK" key in a configuration object.

        :param pdk_root: Required if Volare is not installed.

            If Volare is installed, this value can be used to optionally override
            Volare's default.

        :param scl: A standard cell library to use. If not specified, the PDK's
            default standard cell library will be used instead.

        :returns: A tuple containing a Config object and the design directory.
        """

        loader: Callable = Self._loads
        raw: Union[str, dict] = ""
        if not isinstance(config_in, dict):
            if design_dir is not None:
                raise TypeError(
                    "The argument design_dir is not supported when config_in is not a dictionary."
                )
            config_in = os.path.abspath(config_in)

            design_dir = str(os.path.dirname(config_in))
            config_in = str(config_in)
            if config_in.endswith(".json"):
                raw = open(config_in, encoding="utf8").read()
            elif config_in.endswith(".tcl"):
                raw = open(config_in, encoding="utf8").read()
                loader = Self._loads_tcl
            else:
                if os.path.isdir(config_in):
                    raise ValueError(
                        "Passing design folders as arguments is unsupported in OpenLane 2.0+: please pass the JSON configuration file directly."
                    )
                _, ext = os.path.splitext(config_in)
                raise ValueError(
                    f"Unsupported configuration file extension '{ext}' for '{config_in}'."
                )
        else:
            if design_dir is None:
                raise TypeError(
                    "The argument design_dir is required when using attempting to load a Config with a dictionary."
                )
            raw = config_in
            loader = Self._load_dict

        pdk_root = Self._resolve_pdk_root(pdk_root)

        loaded = loader(
            raw,
            design_dir,
            flow_config_vars=flow_config_vars,
            pdk_root=pdk_root,
            pdk=pdk,
            scl=scl,
            config_override_strings=(config_override_strings or []),
        )

        return (loaded, design_dir)

    @classmethod
    def _loads(
        Self,
        json_str: str,
        *args,
        **kwargs,
    ):
        raw = json.loads(json_str, parse_float=Decimal)
        if "resolve_json" not in kwargs:
            kwargs["resolve_json"] = True
        return Self._load_dict(
            raw,
            *args,
            **kwargs,
        )

    @classmethod
    def _load_dict(
        Self,
        raw: Dict[str, Any],
        design_dir: str,
        flow_config_vars: Sequence[Variable],
        config_override_strings: Sequence[str],
        pdk_root: str,
        pdk: Optional[str] = None,
        scl: Optional[str] = None,
        full_pdk_warnings: bool = False,
        resolve_json: bool = False,
    ) -> "Config":

        meta_raw: Optional[dict] = None
        if raw.get("meta") is not None:
            meta_raw = raw["meta"]
            del raw["meta"]

        for string in config_override_strings:
            key, value = string.split("=", 1)
            raw[key] = value

        process_info = resolve(
            raw,
            only_extract_process_info=True,
            design_dir=design_dir,
        )

        pdk = process_info.get(SpecialKeys.pdk) or pdk
        if pdk is None:
            raise ValueError(
                "The pdk argument is required as the configuration object lacks a 'PDK' key."
            )

        config_in, pdkpath, scl = Self._get_pdk_config(
            pdk=pdk,
            scl=scl,
            pdk_root=pdk_root,
            full_pdk_warnings=full_pdk_warnings,
        )

        config_in = Config(
            config_in,
            overrides=resolve(
                raw,
                pdk=pdk,
                pdkpath=pdkpath,
                scl=config_in[SpecialKeys.scl],
                design_dir=design_dir,
            ),
        )

        config_in, design_warnings, design_errors = config_in.process_variable_list(
            pdk_variables + list(flow_config_vars),
            removed_variables,
        )

        if meta_raw is not None:
            try:
                config_in.meta = Meta(**meta_raw)
            except TypeError as e:
                design_errors.append(f"'meta' object is invalid: {e}")

        if len(design_errors) != 0:
            raise InvalidConfig(
                "design configuration file", design_warnings, design_errors
            )

        if len(design_warnings) > 0:
            info(
                "Loading the design configuration file has generated the following warnings:"
            )
        for warning in design_warnings:
            warn(warning)

        return config_in

    @classmethod
    def _loads_tcl(
        Self,
        config: str,
        design_dir: str,
        flow_config_vars: Sequence[Variable],
        config_override_strings: Sequence[str],  # Unused, kept for API consistency
        pdk_root: str,
        pdk: Optional[str] = None,
        scl: Optional[str] = None,
        full_pdk_warnings: bool = False,
    ) -> "Config":
        warn(
            "Support for .tcl configuration files is deprecated. Please migrate to a .json file at your earliest convenience."
        )

        pdk_root = Self._resolve_pdk_root(pdk_root)

        config_in = Config(
            {
                SpecialKeys.pdk_root: pdk_root,
                SpecialKeys.pdk: pdk,
            }
        )

        tcl_vars_in = dict(config_in)
        tcl_vars_in[SpecialKeys.scl] = ""
        tcl_vars_in[SpecialKeys.design_dir] = design_dir
        tcl_config = env_from_tcl(tcl_vars_in, config)

        process_info = resolve(
            tcl_config,
            only_extract_process_info=True,
            design_dir=design_dir,
        )

        pdk = process_info.get(SpecialKeys.pdk) or pdk

        if pdk is None:
            raise ValueError(
                "The pdk argument is required as the configuration object lacks a 'PDK' key."
            )

        config_in, _, scl = Self._get_pdk_config(
            pdk=pdk,
            scl=scl,
            pdk_root=pdk_root,
            full_pdk_warnings=full_pdk_warnings,
        )

        tcl_vars_in[SpecialKeys.pdk] = pdk
        tcl_vars_in[SpecialKeys.scl] = scl
        tcl_vars_in[SpecialKeys.design_dir] = design_dir

        design_config = env_from_tcl(tcl_vars_in, config)

        config_in = Config(config_in, overrides=design_config)
        for string in config_override_strings:
            key, value = string.split("=", 1)
            config_in[key] = value

        config_in, design_warnings, design_errors = config_in.process_variable_list(
            pdk_variables + list(flow_config_vars),
            removed_variables,
        )

        if len(design_errors) != 0:
            raise InvalidConfig(
                "design configuration file", design_warnings, design_errors
            )

        if len(design_warnings) > 0:
            info(
                "Loading the design configuration file has generated the following warnings:"
            )
        for warning in design_warnings:
            warn(warning)

        return config_in

    @classmethod
    def _resolve_pdk_root(Self, pdk_root: Optional[str]) -> str:
        try:
            import volare

            pdk_root = volare.get_volare_home(pdk_root)
        except ImportError:
            if pdk_root is None:
                raise ValueError(
                    "The pdk_root argument is required as Volare is not installed."
                )
        return os.path.abspath(pdk_root)

    @classmethod
    def _get_pdk_config(
        Self,
        pdk: str,
        scl: Optional[str],
        pdk_root: str,
        full_pdk_warnings: Optional[bool] = False,
    ) -> Tuple["Config", str, str]:
        """
        :returns: A tuple of the PDK configuration, the PDK path and the SCL.
        """

        pdk_config: GenericDict[str, Any] = GenericDict(
            {
                SpecialKeys.pdk_root: pdk_root,
                SpecialKeys.pdk: pdk,
            }
        )
        if scl is not None:
            pdk_config[SpecialKeys.scl] = scl

        pdkpath = os.path.join(pdk_root, pdk)
        if not os.path.exists(pdkpath):
            matches = glob(f"{pdkpath}*")
            errors = [f"The PDK {pdk} was not found."]
            warnings = []
            for match in matches:
                basename = os.path.basename(match)
                warnings.append(f"A similarly-named PDK was found: {basename}")
            raise InvalidConfig("PDK configuration", warnings, errors)

        pdk_config_path = os.path.join(pdkpath, "libs.tech", "openlane", "config.tcl")

        pdk_env = env_from_tcl(
            pdk_config,
            open(pdk_config_path, encoding="utf8").read(),
        )

        scl = pdk_env["STD_CELL_LIBRARY"]
        assert (
            scl is not None
        ), "Fatal error: STD_CELL_LIBRARY default value not set by PDK."

        scl_config_path = os.path.join(
            pdkpath, "libs.tech", "openlane", scl, "config.tcl"
        )

        scl_env = migrate_old_config(
            env_from_tcl(
                pdk_env,
                open(scl_config_path, encoding="utf8").read(),
            )
        )

        config_in = Config(scl_env)
        config_in, pdk_warnings, pdk_errors = config_in.process_variable_list(
            pdk_variables,
            pdk_removed_variables,
        )

        if len(pdk_errors) != 0:
            raise InvalidConfig("PDK configuration files", pdk_warnings, pdk_errors)

        if len(pdk_warnings) > 0:
            if full_pdk_warnings:
                info(
                    "Loading the PDK configuration files has generated the following warnings:"
                )
                for warning in pdk_warnings:
                    warn(warning)

        return (config_in, pdkpath, scl)

    def process_variable_list(
        self,
        variables: Sequence["Variable"],
        removed: Optional[Dict[str, str]] = None,
    ) -> Tuple["Config", List[str], List[str]]:
        """
        Verifies a configuration object against a list of variables, returning
        an object with the variables normalized according to their types.

        :param config: The input, raw configuration object.
        :param variables: A sequence or some other iterable of variables.
        :param removed: A dictionary of variables that may have existed at a point in
            time, but then have gotten removed. Useful to give feedback to the user.
        :returns: A tuple of:
            [0] A final, processed configuration.
            [1] A list of warnings.
            [2] A list of errors.

            If the third element is non-empty, the first object is invalid.
        """
        if removed is None:
            removed = {}

        warnings: List[str] = []
        errors = []
        final: GenericDict[str, Any] = GenericDict()
        mutable = self.copy()

        # Special Deprecation Behaviors
        if (
            mutable.get("DIODE_INSERTION_STRATEGY") is not None
        ):  # Can't use := because 0 is a valid value
            dis = mutable["DIODE_INSERTION_STRATEGY"]
            del mutable["DIODE_INSERTION_STRATEGY"]
            try:
                dis = int(dis)
            except ValueError:
                pass
            if not isinstance(dis, int) or dis in [1, 2, 5] or dis > 6:
                errors.append(
                    f"DIODE_INSERTION_STRATEGY '{dis}' is not available in OpenLane 2. See 'Migrating DIODE_INSERTION_STRATEGY' in the docs for more info."
                )
            else:
                warnings.append(
                    "The DIODE_INSERTION_STRATEGY variable has been deprecated. See 'Migrating DIODE_INSERTION_STRATEGY' in the docs for more info."
                )

                final["GRT_REPAIR_ANTENNAS"] = False
                final["RUN_HEURISTIC_DIODE_INSERTION"] = False
                final["DIODE_ON_PORTS"] = "none"
                if dis in [3, 6]:
                    final["GRT_REPAIR_ANTENNAS"] = True
                if dis in [5, 6]:
                    final["RUN_HEURISTIC_DIODE_INSERTION"] = True
                    final["DIODE_ON_PORTS"] = "in"

        # Macros
        translated_macros = False
        if mutable.get("EXTRA_SPEFS") is not None:
            mutable["MACROS"] = mutable.get("MACROS") or {}

            extra_spef_list = mutable["EXTRA_SPEFS"]
            del mutable["EXTRA_SPEFS"]
            if isinstance(extra_spef_list, str):
                extra_spef_list = extra_spef_list.split(" ")

            if not isinstance(extra_spef_list, list):
                errors.append(
                    f"Invalid type for 'EXTRA_SPEFS': {type(extra_spef_list)}. It is recommended that you update your configuration to use the Macro object."
                )
            elif len(extra_spef_list) % 4 != 0:
                errors.append(
                    "Invalid value for 'EXTRA_SPEFS': Element count not divisible by four. It is recommended that you update your configuration to use the Macro object."
                )
            else:
                translated_macros = True
                warnings.append(
                    "The configuration variable 'EXTRA_SPEFS' is deprecated. Check the docs on how to use the new 'MACROS' configuration variable."
                )
                for i in range(len(extra_spef_list) // 4):
                    start = i * 4
                    module, min, nom, max = (
                        extra_spef_list[start],
                        extra_spef_list[start + 1],
                        extra_spef_list[start + 2],
                        extra_spef_list[start + 3],
                    )
                    macro_dict = {"module": module, "gds": ["/dev/null"]}
                    macro_dict["spef"] = {
                        "min_*": [min],
                        "nom_*": [nom],
                        "max_*": [max],
                    }
                    mutable["MACROS"][module] = macro_dict

        for variable in variables:
            try:
                key, value_processed = variable.compile(
                    mutable_config=mutable,
                    warning_list_ref=warnings,
                    values_so_far=final,
                )
                if key is not None:
                    del mutable[key]
                final[variable.name] = value_processed
            except ValueError as e:
                errors.append(str(e))

        for key in sorted(mutable.keys()):
            assert isinstance(key, str)

            if key in vars(SpecialKeys).values():
                continue
            if key in removed:
                warnings.append(f"'{key}' has been removed: {removed[key]}")
            elif "_OPT" not in key and key != "//":
                warnings.append(f"Unknown key '{key}' provided.")

        if translated_macros:
            for macro in final["MACROS"].values():
                if macro.gds == "/dev/null":
                    macro.gds = Path("")

        return (Config(final), warnings, errors)
