from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Optional


TEMPLATE_SPEC = """
Name:           {name}
Version:        {version}
Release:        {release}
Summary:        Very small RPM
{epoch}

License:        MIT

BuildArch:      noarch

%description
Very small RPM.

%prep

%build

%install
mkdir -p %{{buildroot}}{prefix}/share/doc/{name}
echo "HELLO" > %{{buildroot}}{prefix}/share/doc/{name}/HELLO

%files
{prefix}/share/doc/{name}

%changelog
* Fri Jun 16 2023 Owen Taylor <otaylor@redhat.com - 1-1
- Created
"""


def build_rpm(path: Path, *,
              name: str, version: str, release: str,
              epoch: Optional[str] = None,
              prefix: Optional[str] = "/usr"):

    spec = TEMPLATE_SPEC.format(
        name=name,
        version=version,
        release=release,
        prefix=prefix,
        epoch=f"Epoch: {epoch}" if epoch else ""
    )

    specpath = path / f"{name}.spec"
    with open(specpath, "w") as f:
        f.write(spec)

    with TemporaryDirectory(dir=path) as tempdir:
        temppath = Path(tempdir)

        # We need to define _rpmfilename, since otherwise we'll pick up a value from
        # the system macros, which could be different (when building in Koji, for example)
        subprocess.check_call([
            "rpmbuild",
            "--define", f"_rpmdir {tempdir}",
            "--define", "_rpmfilename %{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}.rpm",
            "-bb", specpath
        ])

        temp_result = next(temppath.iterdir())
        result = path / temp_result.name
        temp_result.rename(result)

    return result
