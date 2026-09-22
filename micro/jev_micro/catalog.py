"""Map inventory for the 35 SMAC-Hard maps shipped with JEV-Star."""

from pathlib import Path
from smac_hard.env.starcraft2.maps.smac_maps import map_param_registry

REPO = Path(__file__).resolve().parents[1]
SUPPORTED_MAPS = tuple(map_param_registry)
DEVELOPMENT_MAPS = {"unit_test", "pvt_large"}


def map_source(name):
    if name not in map_param_registry:
        raise ValueError(f"Unsupported map: {name}")
    candidate = REPO / "SMAC_HARD_maps" / f"{name}.SC2Map"
    if candidate.is_file():
        return candidate
    raise ValueError(f"No map file for registered map: {name}")


def inventory():
    return [{"map": name, **params, "source": str(map_source(name).relative_to(REPO)),
             "development_map": name in DEVELOPMENT_MAPS}
            for name, params in map_param_registry.items()]
