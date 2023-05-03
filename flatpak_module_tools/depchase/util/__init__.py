from .dataset import display_dataset_name, get_default_dataset  # noqa: F401
from .dataset import parse_dataset_name  # noqa: F401
from .dict import DefaultDictWithKey, dict_merge_deep  # noqa: F401


def rpm_name_only(rpm_name):
    return rpm_name.rsplit("-", 2)[0]
