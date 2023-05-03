from .arch import score as arch_score  # noqa: F401
from .click import ChoiceGlob  # noqa: F401
from .dataset import display_dataset_name, get_default_dataset  # noqa: F401
from .dataset import parse_dataset_name  # noqa: F401
from .dict import DefaultDictWithKey, dict_merge_deep  # noqa: F401
from .yaml import yaml_load, yaml_load_all, yaml_safe_load, yaml_safe_load_all  # noqa: F401


def rpm_name_only(rpm_name):
    return rpm_name.rsplit("-", 2)[0]
