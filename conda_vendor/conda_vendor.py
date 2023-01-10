""" The main idea is to pass an isolated environment (environment.yaml)  to
 conda-lock and get a list of packages, locations, and metadata.  We
 download all the packages specified into a solution into the directory
 structure of a Conda channel.  We grab the repodata for each package from
 the original source and write a condensed repodata.json only having our
 vendored packages.
 """
import click
import hashlib
import json
import requests
import struct
import sys
import yaml

from packaging import version
from pathlib import Path
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from ruamel.yaml import YAML
from typing import List, Union

from conda_lock import __version__ as conda_lock_version
from conda_lock.conda_solver import (
    DryRunInstall,
    VersionedDependency,
    FetchAction,
)
from conda_lock.conda_solver import (
    _reconstruct_fetch_actions as reconstruct_fetch_actions,
)
from conda_lock.conda_solver import solve_specs_for_arch
from conda_lock.src_parser import LockSpecification
from conda_lock.src_parser.environment_yaml import parse_environment_file
from conda_lock.virtual_package import (
    FakeRepoData,
    default_virtual_package_repodata,
    virtual_package_repo_from_specification,
)

from conda_vendor.version import __version__


#  conda-lock:
#  the solution returned by conda-lock is essentially a dictionary with
#  {
#       'actions':
#       {
#            'LINK': [ link_actions, ...],
#            'FETCH': [ fetch_actions, ...]
#       }
#  }
#
#  the fetch_actions are essentially a struct with info in repodata.json
#
#  class FetchAction(TypedDict):
#      """
#      FETCH actions include all the entries from the corresponding package's
#      repodata.json
#      """
#
#      channel: str
#      constrains: Optional[List[str]]
#      depends: Optional[List[str]]
#      fn: str
#      md5: str
#      sha256: Optional[str]
#      name: str
#      subdir: str
#      timestamp: int
#      url: str
#      version: str
#
#
#  implementation gotchas:
#
#  cross platform vendoring requires specifying conda virtual_packages __unix,
#  __linux, __osx, __cuda, __glibc, etc.  so there are some shenanigans to get
#  a sensible default virtual_package list from conda-lock.  (This is stored
#  in a temporary directory).  After we solve, we remove all virtual_packages
#  from the solution set.
#
#  the way we write repodata.json for each subdirectory will break if we
#  vendor from multiple sources


def _blue(s, bold=True):
    click.echo(click.style(s, fg="blue", bg="black", bold=bold))


def _cyan(s, bold=True):
    click.echo(click.style(s, fg="cyan", bg="black", bold=bold))


def _green(s, bold=True):
    click.echo(click.style(s, fg="green", bg="black", bold=bold))


def _red(s, bold=True):
    click.echo(click.style(s, fg="red", bg="black", bold=bold))


def _yellow(s, bold=True):
    click.echo(click.style(s, fg="yellow", bg="black", bold=bold))


def _generate_lock_spec(
    environment_file: Path, platform: str
) -> LockSpecification:
    # the function parameters changed in conda-lock 1.3.0
    if version.parse(conda_lock_version) < version.parse("1.3.0"):
        return parse_environment_file(environment_file)
    else:
        return parse_environment_file(environment_file, [platform])


def _get_environment_name(environment_file: Path) -> str:
    """find the name of the environment from the environment.yaml"""

    with open(environment_file, "r") as f:
        _yaml = yaml.safe_load(f)
        return _yaml["name"]


def _get_virtual_packages(
    virtual_package_spec: Union[Path, str] = None
) -> FakeRepoData:
    """return a fake repository object  containing the virtual packages
    specified or the conda-lock defaults if virtual_package_spec is None
    """

    if virtual_package_spec is None:
        return default_virtual_package_repodata()

    if isinstance(virtual_package_spec, str):
        virtual_package_spec = Path(virtual_package_spec)

    return virtual_package_repo_from_specification(virtual_package_spec)


def _get_query_list(lock_spec: LockSpecification) -> List[str]:
    """Go through the LockSpecification object and grab all the packages
    with their versions into a list.

    Parameters
    ----------
    lock_spec: conda_lock.src_parser.LockSpecification
        a lock spec generated by conda_lock from the initial enviroment.yaml

    Returns
    -------
    list
       a formatted list of Python packages with their dependency requirements

    """
    specs = []
    for dep in lock_spec.dependencies:
        if dep.version == "":
            specs.append(f"{dep.name}")
        else:
            if dep.version[0].isnumeric():
                specs.append(f"{dep.name}=={dep.version}")
            else:
                specs.append(f"{dep.name}{dep.version}")
    return specs


def _remove_channel(solution: DryRunInstall, channel: str):
    """Filter a conda-lock solution and remove any packages from the specified
    channel.

    Parameters
    ----------
    solution: conda_lock.conda_solver.DryRunInstall
        valid solution from conda-lock

    channel: str
        the channel to remove.
    """
    fetch = []
    link = []

    for entry in solution["actions"]["FETCH"]:
        if entry["channel"].startswith(channel):
            continue
        fetch.append(entry)

    for entry in solution["actions"]["LINK"]:
        if entry["base_url"] == channel:
            continue
        link.append(entry)

    solution["actions"]["FETCH"] = fetch
    solution["actions"]["LINK"] = link
    return solution


def solve_environment(
    environment_file: Path,
    solver: str,
    platform: str,
    virtual_package_spec: Union[Path, str] = None,
) -> List[FetchAction]:
    """Solve the environment specified in the conda environment_file,
    and return a list of all the required packages with enough metadata
    to generate a repo_data.json.

    Parameters
    ----------
    environment_file: pathlib.Path
        path to the environment.yaml to solve

    solver: str
        which conda is used to solve: conda, miniconda, mamba, etc.

    platform: str
        platform to vendor for: eg. linux-32, win-64, osx-64, etc.

    virtual_package_spec: pathlib.Path or str
        location of a conda-lock virtual package specification yaml.  See
        the documentation for conda-lock for the correct format.  If
        this is None, use the default virtual packages that conda-lock
        provides for {platform}

    Returns
    -------
    list [ conda_lock.conda_solver.FetchAction ]

    """
    assert isinstance(environment_file, Path)

    # generate conda-lock's LockSpecification, this will parse the environment
    # file for us and give us more easily handled requirements.
    lock_spec = _generate_lock_spec(environment_file, platform)
    specs = _get_query_list(lock_spec)

    # find virtual packages or use defaults
    virt_pkgs = _get_virtual_packages(virtual_package_spec)

    _cyan(f"Using Solver: {solver}", bold=False)
    _cyan(f"Solving for Platform: {platform}", bold=False)
    _cyan(f"Solving for Spec: {specs}", bold=False)
    _cyan("Virtual Packages:", bold=False)
    for _, pkg in virt_pkgs.all_repodata[platform]["packages"].items():
        _cyan(f"    {pkg['name']}: {pkg['version']}", bold=False)

    # let conda-lock solve for the environment
    channels = [*lock_spec.channels, virt_pkgs.channel]
    solution = solve_specs_for_arch(solver, channels, specs, platform)
    solution = _remove_channel(solution, virt_pkgs.channel.url)
    if not solution["success"]:
        _red(f"Failed to Solve for {specs}")
        _red(f"Using {solver} for {platform}")
        sys.exit(1)
    _green("Successfull Solve")

    # unfortunately Conda sometimes doesn't fill out the FETCH actions
    # completely so conda-lock will generate the appropriate FETCH action from
    # the LINK action and the repodata.json
    patched_solution = reconstruct_fetch_actions(solver, platform, solution)
    return patched_solution["actions"]["FETCH"]


# see https://stackoverflow.com/questions/21371809/cleanly-setting-max-retries-on-python-requests-get-or-post-method
def _improved_download(url: str):
    """Wrapper arround request.get() to allow for retries

    Parameters
    ----------
    url: str
        url to fetch

    Returns
    -------
    request.Response
        response object containing the fetched file
    """
    session = requests.Session()
    retry = Retry(connect=5, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session.get(url)


def _reconstruct_repodata_json(
    repodata_url: str, dest_dir: Path, package_list: List[FetchAction]
):
    """Given the url of a repodata.json file, walk through the package list
    specified and grab all the metadata about the package from the specified
    repodata.json.  Then write a new repodata.json at dest_dir/repo_data.json
    with the given metadata.

    Parameters
    ----------
    repodata_url: str
        location of the repodata.json describing the solved package metadata

    dest_dir: pathlib.Path
        location where the vendored packages are or will be stored.

    package_list: list [ conda_lock.conda_solver.FetchAction ]
        list of packages (and their metadata) provided by conda-lock

    """
    assert isinstance(dest_dir, Path)

    repo_data = {
        "info": {"subdir": dest_dir.name},
        "packages": {},
        "packages.conda": {},
    }

    valid_names = [pkg["fn"] for pkg in package_list]

    live_repodata_json = _improved_download(repodata_url).json()
    _packages = live_repodata_json.get("packages", {})
    _packages_dot_conda = live_repodata_json.get("packages.conda", {})

    with click.progressbar(
        length=len(_packages) + len(_packages_dot_conda),
        label="Hotfix Patching repodata.json",
    ) as pb:

        for name, entry in _packages.items():
            if name in valid_names:
                repo_data["packages"][name] = entry
            pb.update(1)

        for name, entry in _packages_dot_conda.items():
            if name in valid_names:
                repo_data["packages.conda"][name] = entry
            pb.update(1)

    # write to destination
    dest_file = Path(f"{dest_dir}/repodata.json")
    with dest_file.open("w") as f:
        json.dump(repo_data, f)


def create_repodata_json(
    package_list: List[FetchAction], vendored_root: Path
):
    """Go through the package_list, i.e. the solution provided by conda_lock,
    and generate a new repodata.json at vendored_root/{subdir}, where
    subdir is either noarch or the platform (as specified in the metadata
    in package_list).

    Parameters
    ----------
    package_list: list [ conda_lock.conda_solver.FetchAction ]
        list of packages (and their metadata) provided by conda-lock

    vendored_root: pathlib.Path
        location of the root of the new conda channel
    """
    channels = []
    subdirs = []

    for pkg in package_list:
        channels.append(pkg["channel"])
        subdirs.append(pkg["subdir"])

        _blue(70 * "=")
        _yellow(f"Channel: {pkg['channel']}", bold=False)
        _yellow(f"Package: {pkg['fn']}", bold=False)
        _yellow(f"URL: {pkg['url']}", bold=False)
        _yellow(f"SHA256: {pkg['sha256']}", bold=False)
        _yellow(f"Subdirectory: {pkg['subdir']}", bold=False)
        _yellow(f"Timestamp: {pkg['timestamp']}", bold=False)
        _blue(70 * "=")

    channels = list(set(channels))
    subdirs = list(set(subdirs))

    # patch repodata.json for each channel + subdir

    # this monstrosity just calls reconstruct_repodata for each subdir like
    # "noarch" , "osx-64", "linux-64", with each channel like
    # https://conda-forge/blah/osx-64 or
    #  file:///usr/share/blah/local-channel/osx-64 we decide if they mach if
    # subdir is a substring of the channel
    #
    # TODO: This will break if we try to vendor from more than 1 channel
    for subdir in subdirs:
        for channel in (ch for ch in channels if subdir in ch):
            _red(
                f"Reconstructing repodata.json with Hotfix for {subdir} using {channel}/repodata.json"
            )
            _reconstruct_repodata_json(
                f"{channel}/repodata.json",
                vendored_root / subdir,
                package_list,
            )


# TODO: download and checksum in chunks
# https://stackoverflow.com/questions/16694907/download-large-file-in-python-with-requests
def download_packages(
    package_list: List[FetchAction], vendored_root: Path, platform: str
):
    """For each Conda package specified in package_list.  Fetch the binary
    from the url (in the metadata).  Calculate the checksum and verify
    it with the provided checksum.

    Parameters
    ----------
    package_list: list [ conda_lock.conda_solver.FetchAction ]
        list of packages (and their metadata) provided by conda-lock

    vendored_root: pathlib.Path
        location of the root of the new conda channel

    platform: str
        platform to vendor: e.g. linux-64, osx-32, etc.

    """
    assert isinstance(vendored_root, Path)
    _green("Downloading and Verifying SHA256 Checksums for Solved Packages")

    with click.progressbar(
        package_list, label="Downloading Progress"
    ) as pkgs:
        for pkg in pkgs:
            dest_dir = vendored_root / pkg["subdir"]
            assert dest_dir.exists() and dest_dir.is_dir()

            file_data = _improved_download(pkg["url"]).content

            # verify checksum
            sha256 = hashlib.sha256(file_data).hexdigest()
            if sha256 != pkg["sha256"]:
                sys.exit("SHA256 Checksum Validation Failed")

            with open(dest_dir / pkg["fn"], "wb") as f:
                f.write(file_data)


def yaml_dump_ironbank_manifest(package_list: List[FetchAction]):
    """This generates formatted text to insert into the DoD IronBank's
    hardening_manifest.yaml "resources" block.

    Parameters
    ----------
    package_list: list [ conda_lock.conda_solver.FetchAction ]
        list of packages (and their metadata) provided by conda-lock
    """

    _cyan("Generating Iron Bank resources clause")

    # IronBank formatted 'resources' block
    resources = {
        "resources": [],
    }

    for pkg in package_list:
        validation = {"type": "sha256", "value": pkg["sha256"]}
        resource = {
            "url": pkg["url"],
            "filename": pkg["fn"],
            "validation": validation,
        }

        resources["resources"].append(resource)

    yaml = YAML()
    with open("ib_manifest.yaml", "w") as f:
        ironbank_resources = yaml.dump(resources, f)
    _green("Iron Bank resources list written to ib_manifest.yaml")


###########################################################################
#                                                                         #
#                                main                                     #
#                                                                         #
###########################################################################


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Display help and usage for subcommands, use: conda-vendor [COMMAND] --help"""
    pass


# see https://github.com/conda/conda/blob/248741a843e8ce9283fa94e6e4ec9c2fafeb76fd/conda/base/context.py#L51
def _get_conda_platform(platform=None) -> str:
    """Get the platform string (the string Conda needs) but allow the
    caller to override.

    Parameters
    ----------
    platform: str
        platform in Python's format to be converted to Conda's format if
        it is provided, just use it. Otherwise convert the string to something
        that Conda wants
    """

    if platform is not None:
        return platform

    platform = sys.platform
    bits = struct.calcsize("P") * 8

    _platform_map = {
        "linux2": "linux",
        "linux": "linux",
        "darwin": "osx",
        "win32": "win",
        "zos": "zos",
    }

    return f"{_platform_map[platform]}-{bits}"


###########################################################################
#                                                                         #
#                    conda-vendor vendor                                  #
#                                                                         #
###########################################################################


@click.command(
    "vendor",
    help="Vendor dependencies into a local channel, given an environment file",
)
@click.option(
    "-f", "--file", default=None, help="Path to solvable environment.yaml"
)
@click.option(
    "-s",
    "--solver",
    default="conda",
    help="Solver to use. Examples: conda, mamba, micromamba",
)
@click.option(
    "-p",
    "--platform",
    default=_get_conda_platform(),
    help="Platform to solve for.",
)
@click.option(
    "--virtual-package-spec",
    default=None,
    help="specify virtual packages injected into conda-lock solution",
)
@click.option(
    "--dry-run",
    default=False,
    is_flag=True,
    help="Dry Run. Doesn't Download Packages - Returns Formatted JSON of FETCH Action Packages",
)
@click.option(
    "--ironbank-gen",
    default=False,
    is_flag=True,
    help="Save IronBank Resources 'ib_manifest.yaml' in current directory",
)
def vendor(
    file: str,
    solver: str,
    platform: str,
    virtual_package_spec: str,
    dry_run: bool,
    ironbank_gen: bool,
):
    """Main entry point to vendor a file.  This will (in the general case)
    use conda-lock to solve the environment specified in an environment.yaml
    passed in through file parameter. After a valid solution, it will
    download the corresponding Conda packages to a channel in
    current_dir/environment_name.  It creates a valid repodata.json for
    each of the Conda required subdirectories.

    Parameters
    ----------
    file: str
        location of environment.yaml to solve

    solver: str
        specified Conda solver, e.g. conda, micromamba, mamba

    platform: str
        platform to vendor, e.g. linux-32, linux-64, osx-64, win-32

    virtual_package_spec: str
        location of virtual-packages.yml to be used by conda-lock.  The format
        of the virtual-packages.yml is specified in the conda-lock
        documentation.
        Note: if a file named virutal-packages.yml exist in the directory
        where conda-lock is run it will use that file.
        see (https://github.com/conda-incubator/conda-lock).

    dry_run: bool
        if specified just try to solve the environment and dump the solution
        set to stdout.

    ironbank_gen: bool
        if specified create an Iron Bank resources clause (in a yaml)
        "ib_manifest.yaml" in the current directory
    """
    _green(f"Vendoring Local Channel for file: {file}", bold=False)

    environment_file = Path(file)
    environment_name = _get_environment_name(environment_file)

    vendored_root = Path.cwd() / environment_name
    if vendored_root.exists():
        _red(f"vendored channel destination {vendored_root} already exists")
        sys.exit(1)

    if dry_run:
        _yellow("Dry Run - Will Not Download Files")
    else:
        Path.mkdir(vendored_root)
        Path.mkdir(vendored_root / platform)
        Path.mkdir(vendored_root / "noarch")

    package_list = solve_environment(
        environment_file, solver, platform, virtual_package_spec
    )

    if dry_run:
        _green("Dry Run Complete!")
        click.echo(json.dumps(package_list, indent=4))
        sys.exit(0)

    create_repodata_json(package_list, vendored_root)
    download_packages(package_list, vendored_root, platform)

    _green(f"SHA256 Checksum Validation and Packages Downloaded")
    _green(f"Vendoring Complete!")
    _green(f"Vendored Channel: {vendored_root}")

    if ironbank_gen:
        yaml_dump_ironbank_manifest(package_list)


###########################################################################
#                                                                         #
#              conda-vendor ironbank_gen                                  #
#                                                                         #
###########################################################################


@click.command(
    "ironbank-gen",
    help="Generate Formatted Text to use in IronBank's Hardening Manifest",
)
@click.option("-f", "--file", default=None, help="Path to environment.yaml")
@click.option(
    "-s",
    "--solver",
    default="conda",
    help="Solver to use. conda, mamba, micromamba",
)
@click.option(
    "--platform",
    "-p",
    default=_get_conda_platform(),
    help="Platform to solve for.",
)
@click.option(
    "--virtual-package-spec",
    default=None,
    help="specify virtual packages injected into conda-lock solution",
)
def ironbank_gen(
    file: str, solver: str, platform: str, virtual_package_spec: str
):
    """Create an Iron Bank resources clause (in a yaml) "ib_manifest.yaml" in
    the current directory

    Parameters
    ----------
    file: str
        location of environment.yaml to solve

    solver: str
        specified Conda solver, e.g. conda, micromamba, mamba

    platform: str
        platform to vendor, e.g. linux-32, linux-64, osx-64, win-32

    virtual_package_spec: str
        location of virtual-packages.yml to be used by conda-lock.  The format
        of the virtual-packages.yml is specified in the conda-lock
        documentation.
        Note: if a file named virutal-packages.yml exist in the directory
        where conda-lock is run it will use that file.
        see (https://github.com/conda-incubator/conda-lock).
    """
    package_list = solve_environment(
        Path(file), solver, platform, virtual_package_spec
    )
    yaml_dump_ironbank_manifest(package_list)


###########################################################################
#                                                                         #
#              conda-vendor virtual-packages                              #
#                                                                         #
###########################################################################
@click.command("virtual-packages", help="dump host virtual packages")
@click.option(
    "--solver", default="conda", help="Sover to use. conda, mamba, micromamba"
)
@click.option(
    "-o",
    "--output",
    default=None,
    help="name of file to write.  Default is stdout.",
)
def virtual_packages(solver, output):
    from subprocess import check_output
    from shlex import split

    conda_info = check_output(split(f"{solver} info --json"), text=True)
    _json = json.loads(conda_info)

    packages = _json.get("virtual_pkgs", [])
    if len(packages) == 0:
        _red("Unable to find virtual packages")
        sys.exit(1)

    package_list = {}
    for pkg in packages:
        package_list[pkg[0]] = pkg[1]

    virtual_packages_dict = {
        "subdirs": {f"{_get_conda_platform()}": {"packages": package_list}}
    }

    if output:
        with open(output, "w") as f:
            yaml.dump(virtual_packages_dict, f, indent=4)
    else:
        yaml.dump(virtual_packages_dict, sys.stdout, indent=4)


main.add_command(vendor)
main.add_command(ironbank_gen)
main.add_command(virtual_packages)

if __name__ == "main":
    main()
