"""
Tests for cli.py. These tests exclusively test the logic in cli.py itself
and mock out the actual building, installation, etc. They are not end-to-end tests.
"""

from pathlib import Path
import subprocess
from textwrap import dedent
from unittest import mock

from click.testing import CliRunner
import pytest

from flatpak_module_tools.build_context import AutoBuildContext
from flatpak_module_tools.cli import cli
import flatpak_module_tools.config
from flatpak_module_tools.flatpak_builder import FLATPAK_METADATA_BOTH, FLATPAK_METADATA_LABELS

from .mock_koji import make_config


@pytest.fixture
def profile():
    return make_config().profiles["production"]


APP_CONTAINER_YAML = """\
flatpak:
    id: org.gnome.eog
    branch: stable
    runtime-name: flatpak-runtime
    runtime-version: f39
    packages:
    - eog
    command: eog
    finish-args: |-
        --share=ipc
        --socket=fallback-x11
        --socket=wayland
"""

RUNTIME_CONTAINER_YAML = """\
flatpak:
    id: org.fedoraproject.Platform
    build-runtime: true
    name: f39/flatpak-runtime
    component: flatpak-runtime
    branch: f39
    sdk: org.fedoraproject.Sdk
    packages:
    - abattis-cantarell-fonts
    - abattis-cantarell-vf-fonts
"""


@pytest.fixture
def repo_path(request, tmp_path: Path):
    marker = request.node.get_closest_marker("container_yaml")
    if marker:
        container_yaml = marker.args[0]
    else:
        container_yaml = APP_CONTAINER_YAML

    work_path = tmp_path / "eog"
    work_path.mkdir()
    with open(work_path / "container.yaml", "w") as f:
        f.write(container_yaml)

    return work_path


@pytest.fixture
def repo_path_git(tmp_path: Path, repo_path: Path):
    def CC(*args):
        subprocess.check_call(args, cwd=repo_path)

    CC("git", "init", "-b", "stable")
    CC("git", "config", "user.name", "Jenny Doe")
    CC("git", "config", "user.email", "jenny.doe@example.com")
    CC("git", "add", "container.yaml")
    CC("git", "commit", "-m", "Initial import")

    result = tmp_path / "eog_checkout"
    subprocess.check_call(["git", "clone", "file://" + str(repo_path), result])

    return result


@pytest.fixture
def fixed_arch():
    with mock.patch("flatpak_module_tools.utils._get_rpm_arch", return_value="ppc64le"):
        yield


@pytest.fixture
def rpm_builder_mock():
    with mock.patch("flatpak_module_tools.cli.RpmBuilder", spec_set=True) as m:
        yield m


@pytest.fixture
def container_builder_mock():
    with mock.patch("flatpak_module_tools.cli.ContainerBuilder", spec_set=True) as m:
        yield m


@pytest.fixture
def installer_mock():
    with mock.patch("flatpak_module_tools.cli.Installer", spec_set=True) as m:
        yield m


@pytest.fixture
def isolated_config():
    def reset_config():
        flatpak_module_tools.config._extra_config_files = []
        flatpak_module_tools.config._profile_name = None
        flatpak_module_tools.config._config = None

    reset_config()
    yield
    reset_config()


def expect_success(args):
    runner = CliRunner()
    result = runner.invoke(
        cli, args, catch_exceptions=False
    )

    assert result.exit_code == 0, \
        f"Command failed, output='{result.output.strip()}'"


def expect_error(args, expected_message, exit_failure=True):
    runner = CliRunner()
    result = runner.invoke(
        cli, args, catch_exceptions=False
    )

    if exit_failure:
        assert result.exit_code != 0
    else:
        assert result.exit_code == 0

    assert expected_message in result.output, \
        f"'{expected_message}' not in '{result.output.strip()}'"


@pytest.mark.usefixtures("isolated_config")
def test_global_options(rpm_builder_mock, repo_path, tmp_path):
    config_file = tmp_path / "custom.conf"

    with open(config_file, "w") as f:
        f.write(dedent("""\
            profiles:
                custom:
                    koji_profile: custom
        """))

    expect_success([
        "--path", repo_path,
        "--config", config_file,
        "--profile", "custom",
        "--verbose",
        "build-rpms-local", "--auto"
    ])

    context = rpm_builder_mock.call_args.args[0]
    assert isinstance(context, AutoBuildContext)

    assert context.profile.koji_profile == "custom"


@pytest.mark.usefixtures("isolated_config")
def test_global_options_bad_profile(rpm_builder_mock, repo_path, tmp_path):
    expect_error([
        "--path", repo_path,
        "--profile", "custom",
        "build-rpms-local", "--auto"
    ], "Unknown profile 'custom'")


def test_local_repo_option(rpm_builder_mock, container_builder_mock, tmp_path, repo_path):
    local_repo = tmp_path / "rpms"
    local_repo.mkdir()
    subprocess.check_call(["createrepo_c", "--general-compress-type=gz", local_repo])

    expect_success([
        "--path", repo_path,
        "build-container-local",
        "--local-repo", local_repo
    ])

    context = rpm_builder_mock.call_args.args[0]
    assert isinstance(context, AutoBuildContext)
    assert context.local_repo == local_repo


def test_local_repo_missing_warning(rpm_builder_mock, container_builder_mock, tmp_path, repo_path):
    local_repo = tmp_path / "rpms"

    expect_error([
        "--path", repo_path,
        "build-container-local",
        "--local-repo", local_repo
    ], "warning: No repository at", exit_failure=False)

    context = rpm_builder_mock.call_args.args[0]
    assert isinstance(context, AutoBuildContext)
    assert context.local_repo is None


def test_containerspec_option(rpm_builder_mock, repo_path):
    new_path = repo_path / "container-new.yaml"
    with open(new_path, "w") as f:
        f.write(APP_CONTAINER_YAML)

    expect_success([
        "--path", repo_path,
        "build-rpms-local", "--auto",
        "--containerspec", "container-new.yaml"
    ])

    context = rpm_builder_mock.call_args.args[0]
    assert isinstance(context, AutoBuildContext)
    assert context.container_spec.path == new_path


@pytest.mark.container_yaml("42")
def test_containerspec_invalid(rpm_builder_mock, repo_path):
    expect_error(
        ["--path", repo_path, "build-rpms-local", "--auto"],
        "container.yaml: toplevel content must be a mapping"
    )


@pytest.mark.container_yaml("42")
def test_containerspec_missing(rpm_builder_mock, repo_path):
    (repo_path / "container.yaml").unlink()
    expect_error(
        ["--path", repo_path, "build-rpms-local", "--auto"],
        "No such file or directory"
    )


def test_target_cli(rpm_builder_mock, repo_path):
    expect_success([
        "--path", repo_path,
        "build-rpms-local", "--auto", "--target", "f40-flatpak-candidate"
    ])

    context = rpm_builder_mock.call_args.args[0]
    assert isinstance(context, AutoBuildContext)
    assert context.container_target == "f40-flatpak-candidate"


def test_target_container_yaml(rpm_builder_mock, repo_path):
    expect_success([
        "--path", repo_path,
        "build-rpms-local", "--auto"
    ])

    context = rpm_builder_mock.call_args.args[0]
    assert isinstance(context, AutoBuildContext)
    assert context.container_target == "f39-flatpak-candidate"


@pytest.mark.container_yaml(RUNTIME_CONTAINER_YAML)
def test_target_git_branch(rpm_builder_mock, repo_path_git):
    subprocess.check_call([
        "git", "config", "branch.stable.merge", "refs/heads/f41"
    ], cwd=repo_path_git)

    expect_success(["--path", repo_path_git, "build-rpms-local", "--auto"])

    context = rpm_builder_mock.call_args.args[0]
    assert isinstance(context, AutoBuildContext)
    assert context.container_target == "f41-flatpak-candidate"


@pytest.mark.container_yaml(RUNTIME_CONTAINER_YAML)
def test_target_git_branch_none(rpm_builder_mock, repo_path):
    expect_error(
        ["--path", repo_path, "build-rpms-local", "--auto"],
        "Cannot determine git merge branch. "
        "Must set flatpak:runtime_version in container.yaml or specify --target"
    )


@pytest.mark.container_yaml(RUNTIME_CONTAINER_YAML)
def test_target_git_branch_nonnumeric(rpm_builder_mock, repo_path_git):
    expect_error(
        ["--path", repo_path_git, "build-rpms-local", "--auto"],
        "Cannot determine release from branch 'stable'. "
        "Must set flatpak:runtime_version in container.yaml or specify --target"
    )


@pytest.mark.parametrize('cli_options,test_flags', [
    ([], []),
    ([], ["fail"]),
    (["--nowait"], []),
    (["--scratch", "--arch=x86_64", "--arch=ppc64le"], []),
    (["--arch=x86_64"], ["invalid_arch"]),
    (["--skip-tag"], []),
])
@pytest.mark.usefixtures("fixed_arch")
@mock.patch("flatpak_module_tools.cli.watch_koji_task", spec_set=True)
def test_build_container(watch_koji_task_mock: mock.Mock,
                         rpm_builder_mock: mock.Mock,
                         repo_path_git, profile, cli_options, test_flags):
    runner = CliRunner()

    with mock.patch("flatpak_module_tools.cli.get_profile", return_value=profile), \
         mock.patch.object(profile.koji_session, "logged_in", True, create=True), \
         mock.patch.object(profile.koji_session, "flatpakBuild", create=True, return_value=42) \
            as flatpak_build_mock:

        if "fail" in test_flags:
            watch_koji_task_mock.return_value = False

        result = runner.invoke(
            cli,
            ["--path", repo_path_git, "build-container"] + cli_options,
            catch_exceptions=False)

        if "fail" in test_flags:
            assert result.exit_code != 0
        elif "invalid_arch" in test_flags:
            assert result.exit_code != 0
            assert "--arch can only be specified for scratch builds" in result.output
            return
        else:
            assert result.exit_code == 0

        rpm_builder_mock.assert_called_once_with(
            mock.ANY,
            workdir=repo_path_git / "ppc64le/work"
        )
        rpm_builder_mock.return_value.check.assert_called_once_with(
            include_localrepo=False, allow_outdated=False
        )

        src = mock.ANY
        target = "f39-flatpak-candidate"
        opts = {}
        if "--scratch" in cli_options:
            opts["scratch"] = True
            opts["arch_override"] = "x86_64 ppc64le"
        if "--skip-tag" in cli_options:
            opts["skip_tag"] = True
        priority = 5 if "--background" in cli_options else None

        flatpak_build_mock.assert_called_with(src, target, opts=opts, priority=priority)

        if "--nowait" in cli_options:
            watch_koji_task_mock.assert_not_called()
        else:
            watch_koji_task_mock.assert_called_with(mock.ANY, 42)


@pytest.mark.usefixtures("fixed_arch")
def test_build_container_local(rpm_builder_mock, container_builder_mock, installer_mock, repo_path):
    container_builder_mock.return_value.build.return_value = "./foo.oci.tar"

    expect_success(["--path", repo_path, "build-container-local", "--install"])

    rpm_builder_mock.assert_called_once_with(
        mock.ANY,
        workdir=repo_path / "ppc64le/work"
    )
    rpm_builder_mock.return_value.check.assert_called_once_with(
        include_localrepo=True, allow_outdated=False
    )

    container_builder_mock.assert_called_once_with(
        mock.ANY, flatpak_metadata=FLATPAK_METADATA_BOTH
    )
    container_builder_mock.return_value.build.assert_called_once_with(
        workdir=repo_path / "ppc64le/work/oci",
        resultdir=repo_path / "ppc64le/result"
    )

    installer_mock.return_value.set_source_path.assert_called_once_with("./foo.oci.tar")
    installer_mock.return_value.install.assert_called_once_with()


def test_assemble_manual(container_builder_mock, repo_path):
    expect_success([
        "--path", repo_path, "assemble",
        "--nvr", "eog-45.1-1",
        "--runtime-nvr", "flatpak-runtime-39-1",
        "--runtime-repo", "42",
        "--app-repo", "43",
    ])

    container_builder_mock.assert_called_once_with(
        mock.ANY, flatpak_metadata=FLATPAK_METADATA_LABELS
    )
    container_builder_mock.return_value.assemble.assert_called_once_with(
        installroot=Path("/contents"), workdir=Path("/tmp"), resultdir=Path(".")
    )


@pytest.mark.parametrize("args,error_message", [(
    [
        "--target", "f39-flatpak-candidate",
        "--nvr", "eog-45.1-1",
    ], "--target cannot be specified together with "
       "--nvr, --runtime-nvr, --runtime-repo, or --app-repo"), (
    [
        "--nvr", "eog-45.1-1",
    ], "--nvr, --runtime-nvr, --runtime-repo, and --app-repo "
       "must be specified for applications"
)])
def test_assemble_manual_invalid(container_builder_mock, repo_path, args, error_message):
    expect_error(["--path", repo_path, "assemble"] + args, error_message)


@pytest.mark.container_yaml(RUNTIME_CONTAINER_YAML)
@pytest.mark.parametrize("args,error_message", [(
    [
        "--nvr", "eog-45.1-1",
    ], "--nvr and --runtime-repo must be specified for runtimes"), (
    [
        "--nvr", "eog-45.1-1",
        "--runtime-repo", "42",
        "--app-repo", "43",
    ], "--runtime-nvr and --app-repo must not be specified for runtimes"
)])
def test_assemble_manual_invalid_runtime(container_builder_mock, repo_path, args, error_message):
    expect_error(["--path", repo_path, "assemble"] + args, error_message)


def test_assemble_auto(container_builder_mock, repo_path):
    expect_success(["--path", repo_path, "assemble"])

    container_builder_mock.assert_called_once_with(
        mock.ANY, flatpak_metadata=FLATPAK_METADATA_LABELS
    )
    container_builder_mock.return_value.assemble.assert_called_once_with(
        installroot=Path("/contents"), workdir=Path("/tmp"), resultdir=Path(".")
    )


def test_installer(installer_mock):
    expect_success(["install", "./foo.oci.tar"])
    installer_mock.return_value.set_source_path.assert_called_once_with("./foo.oci.tar")
    installer_mock.return_value.install.assert_called_once_with()
    installer_mock.reset_mock()

    expect_success(["install", "https://example.com/foo.oci.tar"])
    installer_mock.return_value.set_source_url.assert_called_once_with(
        "https://example.com/foo.oci.tar"
    )
    installer_mock.return_value.install.assert_called_once_with()
    installer_mock.reset_mock()

    expect_success(["install", "--koji", "eog:stable"])
    installer_mock.return_value.set_source_koji_name_stream.assert_called_once_with(
        "eog:stable"
    )
    installer_mock.return_value.install.assert_called_once_with()
    installer_mock.reset_mock()


@pytest.mark.usefixtures("fixed_arch")
def test_build_rpms(rpm_builder_mock, repo_path):
    expect_success(["--path", repo_path, "build-rpms", "eog", "--auto"])

    workdir = repo_path / "ppc64le/work"
    rpm_builder_mock.assert_called_once_with(mock.ANY, workdir=workdir)

    context = rpm_builder_mock.call_args.args[0]
    assert isinstance(context, AutoBuildContext)

    assert context.container_target == "f39-flatpak-candidate"

    build_rpms = rpm_builder_mock.return_value.build_rpms
    build_rpms.assert_called_once_with(("eog",), auto=True, allow_outdated=False)


@pytest.mark.usefixtures("fixed_arch")
def test_build_rpms_local(rpm_builder_mock, repo_path):
    expect_success(["--path", repo_path, "build-rpms-local", "eog", "../libtastypng", "--auto"])

    workdir = repo_path / "ppc64le/work"
    rpm_builder_mock.assert_called_once_with(mock.ANY, workdir=workdir)

    build_rpms_local = rpm_builder_mock.return_value.build_rpms_local
    build_rpms_local.assert_called_once_with(
        ["eog"], [Path("../libtastypng")], auto=True, allow_outdated=False
    )


@pytest.mark.parametrize("subcommand", ("build-rpms", "build-rpms-local"))
def test_build_rpms_no_packages(rpm_builder_mock, subcommand, repo_path):
    expect_error(
        ["--path", repo_path, subcommand],
        "Nothing to rebuild, specify packages or --auto",
        exit_failure=False
    )
