import koji
import re

FEDORA_TAG_PATTERNS = [(re.compile(p), s) for p, s in [
    (r'^(f\d+)-modular$', 'stable'),
    (r'^(f\d+)-modular-pending$', 'pending'),
    (r'^(f\d+)-modular-signing-pending$', 'signing-pending'),
    (r'^(f\d+)-modular-updates$', 'stable'),
    (r'^(f\d+)-modular-updates-candidate$', 'candidate'),
    (r'^(f\d+)-modular-updates-pending$', 'pending'),
    (r'^(f\d+)-modular-updates-testing$', 'testing'),
]]

STATUSES = sorted({s for p, s in FEDORA_TAG_PATTERNS})


def _add_status_and_base_version(session, build):
    # Find out where a build is tagged to find its base Fedora version and status
    tags = session.listTags(build=build['build_id'])
    for t in tags:
        for p, s in FEDORA_TAG_PATTERNS:
            m = p.match(t['name'])
            if m:
                build['gmb_status'] = s
                build['gmb_base_version'] = m.group(1)
                break


def _add_rpm_list(session, build):
    archives = session.listArchives(buildID=build['build_id'])
    assert len(archives) == 1
    build['gmb_rpms'] = session.listRPMs(imageID=archives[0]['id'])


def get_module_builds_for_session(session,
                                  module_name, stream,
                                  version=None,
                                  base_version=None,
                                  status=None,
                                  include_rpms=False):

    """Like get_module_builds() but takes and existing Koji session object

    session -- a koji.ClientSession object
    module_name -- the name of the module
    stream -- the stream of the module
    version -- the version of the module. If not specified, the latest
       version will be used.
    base_version -- the base Fedora version that the module was built for
       (corresponds to the stream of the 'platform' module). If None
       builds for all base versions will be returned
    status -- the status of the module in Fedora - can be
       'stable', 'testing', 'candidate', 'pending', or 'signing-ending'. If None,
       builds with all statuses will be returned
    include_rpms -- if %TRUE, each returned build will have a 'gmb_rpms' key with
       the value being a list of dictionaries with information about each RPM
       built in the module.
    """

    package_id = session.getPackageID(module_name)

    # List all builds of the module
    builds = session.listBuilds(packageID=package_id, type='module',
                                state=koji.BUILD_STATES['COMPLETE'])
    # For convenience, add keys that turn the NVR into module terms
    for b in builds:
        b['gmb_stream'] = b['version']
        if '.' in b['release']:
            b['gmb_version'], b['gmb_context'] = b['release'].split('.', 1)
        else:
            b['gmb_version'], b['gmb_context'] = b['release'], None

        # Will fill in later
        b['gmb_status'] = None
        b['gmb_base_version'] = None

    def filter_build(b):
        if b['gmb_stream'] != stream:
            return False
        if version is not None and b['gmb_version'] != version:
            return False
        return True

    matching_builds = [b for b in builds if filter_build(b)]

    if base_version is not None or status is not None:
        for b in matching_builds:
            _add_status_and_base_version(session, b)

        def filter_build_2(b):
            if base_version is not None and b['gmb_base_version'] != base_version:
                return False
            if status is not None and b['gmb_status'] != status:
                return False
            return True

        matching_builds = [b for b in matching_builds if filter_build_2(b)]

    if version is None and len(matching_builds) > 0:
        # OK, we've limited the builds to the ones that match the search criteria, find
        # the most recent one, based on the module version.
        latest_version = max(b['gmb_version'] for b in matching_builds)
        result = [b for b in matching_builds if b['gmb_version'] == latest_version]
    else:
        result = matching_builds

    # Look up tag information if we didn't do so earlier to filter
    if not (base_version is not None or status is not None):
        for b in result:
            _add_status_and_base_version(session, b)

    if include_rpms:
        for b in result:
            _add_rpm_list(session, b)

    return result


def get_module_builds(module_name, stream,
                      version=None,
                      base_version=None,
                      status=None,
                      koji_config=None,
                      koji_profile='koji',
                      include_rpms=False):

    """Return a list of Koji build objects for the specified, or latest
    version of a module. All the returned builds will have the same version,
    but multiple builds with different contexts may be returned due to
    stream expansion.

    module_name -- the name of the module
    stream -- the stream of the module
    version -- the version of the module. If not specified, the latest
       version will be used.
    base_version -- the base Fedora version that the module was built for
       (corresponds to the stream of the 'platform' module). If None
       builds for all base versions will be returned
    status -- the status of the module in Fedora - can be
       'stable', 'testing', 'candidate', 'pending', or 'signing-ending'. If None,
       builds with all statuses will be returned
    koji_config -- alternate koji config file to read
    koji_profile -- alternate koji profile to use
    include_rpms -- if %TRUE, each returned build will have a 'gmb_rpms' key with
       the value being a list of dictionaries with information about each RPM
       built in the module.
    """

    options = koji.read_config(profile_name=koji_profile, user_config=koji_config)
    session_opts = koji.grab_session_options(options)
    session = koji.ClientSession(options['server'], session_opts)

    return get_module_builds_for_session(session,
                                         module_name, stream,
                                         version=version,
                                         base_version=base_version,
                                         status=status,
                                         include_rpms=include_rpms)
