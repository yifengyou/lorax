# Copyright (C) 2018-2019 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
""" Setup for composing an image

Adding New Output Types
-----------------------

The new output type must add a kickstart template to ./share/composer/ where the
name of the kickstart (without the trailing .ks) matches the entry in compose_args.

The kickstart should not have any url or repo entries, these will be added at build
time. The %packages section should be the last thing, and while it can contain mandatory
packages required by the output type, it should not have the trailing %end because the
package NEVRAs will be appended to it at build time.

compose_args should have a name matching the kickstart, and it should set the novirt_install
parameters needed to generate the desired output. Other types should be set to False.

"""
import logging
log = logging.getLogger("lorax-composer")

import os
from glob import glob
from io import StringIO
from math import ceil
import pytoml as toml
import shutil
from uuid import uuid4

# Use pykickstart to calculate disk image size
from pykickstart.parser import KickstartParser
from pykickstart.version import makeVersion

from pylorax import ArchData, find_templates, get_buildarch
from pylorax.api.gitrpm import create_gitrpm_repo
from pylorax.api.projects import projects_depsolve, projects_depsolve_with_size, dep_nevra
from pylorax.api.projects import ProjectsError
from pylorax.api.recipes import read_recipe_and_id
from pylorax.api.timestamp import TS_CREATED, write_timestamp
from pylorax.base import DataHolder
from pylorax.imgutils import default_image_name
from pylorax.ltmpl import LiveTemplateRunner
from pylorax.sysutils import joinpaths, flatconfig


def test_templates(dbo, share_dir):
    """ Try depsolving each of the the templates and report any errors

    :param dbo: dnf base object
    :type dbo: dnf.Base
    :returns: List of template types and errors
    :rtype: List of errors

    Return a list of templates and errors encountered or an empty list
    """
    template_errors = []
    for compose_type, enabled in compose_types(share_dir):
        if not enabled:
            continue

        # Read the kickstart template for this type
        ks_template_path = joinpaths(share_dir, "composer", compose_type) + ".ks"
        ks_template = open(ks_template_path, "r").read()

        # How much space will the packages in the default template take?
        ks_version = makeVersion()
        ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)
        ks.readKickstartFromString(ks_template+"\n%end\n")
        pkgs = [(name, "*") for name in ks.handler.packages.packageList]
        grps = [grp.name for grp in ks.handler.packages.groupList]
        try:
            projects_depsolve(dbo, pkgs, grps)
        except ProjectsError as e:
            template_errors.append("Error depsolving %s: %s" % (compose_type, str(e)))

    return template_errors


def repo_to_ks(r, url="url"):
    """ Return a kickstart line with the correct args.
    :param r: DNF repository information
    :type r: dnf.Repo
    :param url: "url" or "baseurl" to use for the baseurl parameter
    :type url: str
    :returns: kickstart command arguments for url/repo command
    :rtype: str

    Set url to "baseurl" if it is a repo, leave it as "url" for the installation url.
    """
    cmd = ""
    # url uses --url not --baseurl
    if r.baseurl:
        cmd += '--%s="%s" ' % (url, r.baseurl[0])
    elif r.metalink:
        cmd += '--metalink="%s" ' % r.metalink
    elif r.mirrorlist:
        cmd += '--mirrorlist="%s" ' % r.mirrorlist
    else:
        raise RuntimeError("Repo has no baseurl, metalink, or mirrorlist")

    if r.proxy:
        cmd += '--proxy="%s" ' % r.proxy

    if not r.sslverify:
        cmd += '--noverifyssl'

    if r.sslcacert:
        cmd += ' --sslcacert="%s"' % r.sslcacert
    if r.sslclientcert:
        cmd += ' --sslclientcert="%s"' % r.sslclientcert
    if r.sslclientkey:
        cmd += ' --sslclientkey="%s"' % r.sslclientkey

    return cmd


def bootloader_append(line, kernel_append):
    """ Insert the kernel_append string into the --append argument

    :param line: The bootloader ... line
    :type line: str
    :param kernel_append: The arguments to append to the --append section
    :type kernel_append: str

    Using pykickstart to process the line is the best way to make sure it
    is parsed correctly, and re-assembled for inclusion into the final kickstart
    """
    ks_version = makeVersion()
    ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)
    ks.readKickstartFromString(line)

    if ks.handler.bootloader.appendLine:
        ks.handler.bootloader.appendLine += " %s" % kernel_append
    else:
        ks.handler.bootloader.appendLine = kernel_append

    # Converting back to a string includes a comment, return just the bootloader line
    return str(ks.handler.bootloader).splitlines()[-1]


def get_kernel_append(recipe):
    """Return the customizations.kernel append value

    :param recipe:
    :type recipe: Recipe object
    :returns: append value or empty string
    :rtype: str
    """
    if "customizations" not in recipe or \
       "kernel" not in recipe["customizations"] or \
       "append" not in recipe["customizations"]["kernel"]:
        return ""
    return recipe["customizations"]["kernel"]["append"]


def timezone_cmd(line, settings):
    """ Update the timezone line with the settings

    :param line: The timezone ... line
    :type line: str
    :param settings: A dict with timezone and/or ntpservers list
    :type settings: dict

    Using pykickstart to process the line is the best way to make sure it
    is parsed correctly, and re-assembled for inclusion into the final kickstart
    """
    ks_version = makeVersion()
    ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)
    ks.readKickstartFromString(line)

    if "timezone" in settings:
        ks.handler.timezone.timezone = settings["timezone"]
    if "ntpservers" in settings:
        ks.handler.timezone.ntpservers = settings["ntpservers"]

    # Converting back to a string includes a comment, return just the timezone line
    return str(ks.handler.timezone).splitlines()[-1]


def get_timezone_settings(recipe):
    """Return the customizations.timezone dict

    :param recipe:
    :type recipe: Recipe object
    :returns: append value or empty string
    :rtype: dict
    """
    if "customizations" not in recipe or \
       "timezone" not in recipe["customizations"]:
        return {}
    return recipe["customizations"]["timezone"]


def lang_cmd(line, languages):
    """ Update the lang line with the languages

    :param line: The lang ... line
    :type line: str
    :param settings: The list of languages
    :type settings: list

    Using pykickstart to process the line is the best way to make sure it
    is parsed correctly, and re-assembled for inclusion into the final kickstart
    """
    ks_version = makeVersion()
    ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)
    ks.readKickstartFromString(line)

    if languages:
        ks.handler.lang.lang = languages[0]

        if len(languages) > 1:
            ks.handler.lang.addsupport = languages[1:]

    # Converting back to a string includes a comment, return just the lang line
    return str(ks.handler.lang).splitlines()[-1]


def get_languages(recipe):
    """Return the customizations.locale.languages list

    :param recipe: The recipe
    :type recipe: Recipe object
    :returns: list of language strings
    :rtype: list
    """
    if "customizations" not in recipe or \
       "locale" not in recipe["customizations"] or \
       "languages" not in recipe["customizations"]["locale"]:
        return []
    return recipe["customizations"]["locale"]["languages"]


def keyboard_cmd(line, layout):
    """ Update the keyboard line with the layout

    :param line: The keyboard ... line
    :type line: str
    :param settings: The keyboard layout
    :type settings: str

    Using pykickstart to process the line is the best way to make sure it
    is parsed correctly, and re-assembled for inclusion into the final kickstart
    """
    ks_version = makeVersion()
    ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)
    ks.readKickstartFromString(line)

    if layout:
        ks.handler.keyboard.keyboard = layout
        ks.handler.keyboard.vc_keymap = ""
        ks.handler.keyboard.x_layouts = []

    # Converting back to a string includes a comment, return just the keyboard line
    return str(ks.handler.keyboard).splitlines()[-1]


def get_keyboard_layout(recipe):
    """Return the customizations.locale.keyboard list

    :param recipe: The recipe
    :type recipe: Recipe object
    :returns: The keyboard layout string
    :rtype: str
    """
    if "customizations" not in recipe or \
       "locale" not in recipe["customizations"] or \
       "keyboard" not in recipe["customizations"]["locale"]:
        return []
    return recipe["customizations"]["locale"]["keyboard"]


def firewall_cmd(line, settings):
    """ Update the firewall line with the new ports and services

    :param line: The firewall ... line
    :type line: str
    :param settings: A dict with the list of services and ports to enable and disable
    :type settings: dict

    Using pykickstart to process the line is the best way to make sure it
    is parsed correctly, and re-assembled for inclusion into the final kickstart
    """
    ks_version = makeVersion()
    ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)
    ks.readKickstartFromString(line)

    # Do not override firewall --disabled
    if ks.handler.firewall.enabled != False and settings:
        ks.handler.firewall.ports = sorted(set(settings["ports"] + ks.handler.firewall.ports))
        ks.handler.firewall.services = sorted(set(settings["enabled"] + ks.handler.firewall.services))
        ks.handler.firewall.remove_services = sorted(set(settings["disabled"] + ks.handler.firewall.remove_services))

    # Converting back to a string includes a comment, return just the keyboard line
    return str(ks.handler.firewall).splitlines()[-1]


def get_firewall_settings(recipe):
    """Return the customizations.firewall settings

    :param recipe: The recipe
    :type recipe: Recipe object
    :returns: A dict of settings
    :rtype: dict
    """
    settings = {"ports": [], "enabled": [], "disabled": []}

    if "customizations" not in recipe or \
       "firewall" not in recipe["customizations"]:
        return settings

    settings["ports"] = recipe["customizations"]["firewall"].get("ports", [])

    if "services" in recipe["customizations"]["firewall"]:
        settings["enabled"] = recipe["customizations"]["firewall"]["services"].get("enabled", [])
        settings["disabled"] = recipe["customizations"]["firewall"]["services"].get("disabled", [])
    return settings


def services_cmd(line, settings):
    """ Update the services line with additional services to enable/disable

    :param line: The services ... line
    :type line: str
    :param settings: A dict with the list of services to enable and disable
    :type settings: dict

    Using pykickstart to process the line is the best way to make sure it
    is parsed correctly, and re-assembled for inclusion into the final kickstart
    """
    # Empty services and no additional settings, return an empty string
    if not line and not settings["enabled"] and not settings["disabled"]:
        return ""

    ks_version = makeVersion()
    ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)

    # Allow passing in a 'default' so that the enable/disable may be applied to it, without
    # parsing it and emitting a kickstart error message
    if line != "services":
        ks.readKickstartFromString(line)

    # Add to any existing services, removing any duplicates
    ks.handler.services.enabled = sorted(set(settings["enabled"] + ks.handler.services.enabled))
    ks.handler.services.disabled = sorted(set(settings["disabled"] + ks.handler.services.disabled))

    # Converting back to a string includes a comment, return just the keyboard line
    return str(ks.handler.services).splitlines()[-1]


def get_services(recipe):
    """Return the customizations.services settings

    :param recipe: The recipe
    :type recipe: Recipe object
    :returns: A dict of settings
    :rtype: dict
    """
    settings = {"enabled": [], "disabled": []}

    if "customizations" not in recipe or \
       "services" not in recipe["customizations"]:
        return settings

    settings["enabled"] = sorted(recipe["customizations"]["services"].get("enabled", []))
    settings["disabled"] = sorted(recipe["customizations"]["services"].get("disabled", []))
    return settings


def get_default_services(recipe):
    """Get the default string for services, based on recipe
    :param recipe: The recipe

    :type recipe: Recipe object
    :returns: string with "services" or ""
    :rtype: str

    When no services have been selected we don't need to add anything to the kickstart
    so return an empty string. Otherwise return "services" which will be updated with
    the settings.
    """
    services = get_services(recipe)

    if services["enabled"] or services["disabled"]:
        return "services"
    else:
        return ""


def customize_ks_template(ks_template, recipe):
    """ Customize the kickstart template and return it

    :param ks_template: The kickstart template
    :type ks_template: str
    :param recipe:
    :type recipe: Recipe object

    Apply customizations to existing template commands, or add defaults for ones that are
    missing and required.

    Apply customizations.kernel.append to the bootloader argument in the template.
    Add bootloader line if it is missing.

    Add default timezone if needed. It does NOT replace an existing timezone entry
    """
    # Commands to be modified [NEW-COMMAND-FUNC, NEW-VALUE, DEFAULT, REPLACE]
    # The function is called with a kickstart command string and the value to replace
    # The value is specific to the command, and is understood by the function
    # The default is a complete kickstart command string, suitable for writing to the template
    # If REPLACE is False it will not change an existing entry only add a missing one
    commands = {"bootloader": [bootloader_append,
                               get_kernel_append(recipe),
                               'bootloader --location=none', True],
                "timezone":   [timezone_cmd,
                               get_timezone_settings(recipe),
                               'timezone UTC', False],
                "lang":       [lang_cmd,
                               get_languages(recipe),
                               'lang en_US.UTF-8', True],
                "keyboard":   [keyboard_cmd,
                               get_keyboard_layout(recipe),
                               'keyboard --xlayouts us --vckeymap us', True],
                "firewall":   [firewall_cmd,
                               get_firewall_settings(recipe),
                               'firewall --enabled', True],
                "services":   [services_cmd,
                               get_services(recipe),
                               get_default_services(recipe), True]
               }
    found = {}

    output = StringIO()
    for line in ks_template.splitlines():
        for cmd in commands:
            (new_command, value, default, replace) = commands[cmd]
            if line.startswith(cmd):
                found[cmd] = True
                if value and replace:
                    log.debug("Replacing %s with %s", cmd, value)
                    print(new_command(line, value), file=output)
                else:
                    log.debug("Skipping %s", cmd)
                    print(line, file=output)
                break
        else:
            # No matches, write the line as-is
            print(line, file=output)

    # Write out defaults for the ones not found
    # These must go FIRST because the template still needs to have the packages added
    defaults = StringIO()
    for cmd in commands:
        if cmd in found:
            continue
        (new_command, value, default, _) = commands[cmd]
        if value and default:
            log.debug("Setting %s to use %s", cmd, value)
            print(new_command(default, value), file=defaults)
        elif default:
            log.debug("Setting %s to %s", cmd, default)
            print(default, file=defaults)

    return defaults.getvalue() + output.getvalue()


def write_ks_root(f, user):
    """ Write kickstart root password and sshkey entry

    :param f: kickstart file object
    :type f: open file object
    :param user: A blueprint user dictionary
    :type user: dict
    :returns: True if it wrote a rootpw command to the kickstart
    :rtype: bool

    If the entry contains a ssh key, use sshkey to write it
    If it contains password, use rootpw to set it

    root cannot be used with the user command. So only key and password are supported
    for root.
    """
    wrote_rootpw = False

    # ssh key uses the sshkey kickstart command
    if "key" in user:
        f.write('sshkey --user %s "%s"\n' % (user["name"], user["key"]))

    if "password" in user:
        if any(user["password"].startswith(prefix) for prefix in ["$2b$", "$6$", "$5$"]):
            log.debug("Detected pre-crypted password")
            f.write('rootpw --iscrypted "%s"\n' % user["password"])
            wrote_rootpw = True
        else:
            log.debug("Detected plaintext password")
            f.write('rootpw --plaintext "%s"\n' % user["password"])
            wrote_rootpw = True

    return wrote_rootpw

def write_ks_user(f, user):
    """ Write kickstart user and sshkey entry

    :param f: kickstart file object
    :type f: open file object
    :param user: A blueprint user dictionary
    :type user: dict

    If the entry contains a ssh key, use sshkey to write it
    All of the user fields are optional, except name, write out a kickstart user entry
    with whatever options are relevant.
    """
    # ssh key uses the sshkey kickstart command
    if "key" in user:
        f.write('sshkey --user %s "%s"\n' % (user["name"], user["key"]))

    # Write out the user kickstart command, much of it is optional
    f.write("user --name %s" % user["name"])
    if "home" in user:
        f.write(" --homedir %s" % user["home"])

    if "password" in user:
        if any(user["password"].startswith(prefix) for prefix in ["$2b$", "$6$", "$5$"]):
            log.debug("Detected pre-crypted password")
            f.write(" --iscrypted")
        else:
            log.debug("Detected plaintext password")
            f.write(" --plaintext")

        f.write(" --password \"%s\"" % user["password"])

    if "shell" in user:
        f.write(" --shell %s" % user["shell"])

    if "uid" in user:
        f.write(" --uid %d" % int(user["uid"]))

    if "gid" in user:
        f.write(" --gid %d" % int(user["gid"]))

    if "description" in user:
        f.write(" --gecos \"%s\"" % user["description"])

    if "groups" in user:
        f.write(" --groups %s" % ",".join(user["groups"]))

    f.write("\n")


def write_ks_group(f, group):
    """ Write kickstart group entry

    :param f: kickstart file object
    :type f: open file object
    :param group: A blueprint group dictionary
    :type user: dict

    gid is optional
    """
    if "name" not in group:
        raise RuntimeError("group entry requires a name")

    f.write("group --name %s" % group["name"])
    if "gid" in group:
        f.write(" --gid %d" % int(group["gid"]))

    f.write("\n")


def add_customizations(f, recipe):
    """ Add customizations to the kickstart file

    :param f: kickstart file object
    :type f: open file object
    :param recipe:
    :type recipe: Recipe object
    :returns: None
    :raises: RuntimeError if there was a problem writing to the kickstart
    """
    if "customizations" not in recipe:
        f.write('rootpw --lock\n')
        return
    customizations = recipe["customizations"]

    # allow customizations to be incorrectly specified as [[customizations]] instead of [customizations]
    if isinstance(customizations, list):
        customizations = customizations[0]

    if "hostname" in customizations:
        f.write("network --hostname=%s\n" % customizations["hostname"])

    # TODO - remove this, should use user section to define this
    if "sshkey" in customizations:
        # This is a list of entries
        for sshkey in customizations["sshkey"]:
            if "user" not in sshkey or "key" not in sshkey:
                log.error("%s is incorrect, skipping", sshkey)
                continue
            f.write('sshkey --user %s "%s"\n' % (sshkey["user"], sshkey["key"]))

    # Creating a user also creates a group. Make a list of the names for later
    user_groups = []
    # kickstart requires a rootpw line
    wrote_rootpw = False
    if "user" in customizations:
        # only name is required, everything else is optional
        for user in customizations["user"]:
            if "name" not in user:
                raise RuntimeError("user entry requires a name")

            # root is special, cannot use normal user command for it
            if user["name"] == "root":
                wrote_rootpw = write_ks_root(f, user)
                continue

            write_ks_user(f, user)
            user_groups.append(user["name"])

    if "group" in customizations:
        for group in customizations["group"]:
            if group["name"] not in user_groups:
                write_ks_group(f, group)
            else:
                log.warning("Skipping group %s, already created by user", group["name"])

    # Lock the root account if no root user password has been specified
    if not wrote_rootpw:
        f.write('rootpw --lock\n')


def get_extra_pkgs(dbo, share_dir, compose_type):
    """Return extra packages needed for the output type

    :param dbo: dnf base object
    :type dbo: dnf.Base
    :param share_dir: Path to the top level share directory
    :type share_dir: str
    :param compose_type: The type of output to create from the recipe
    :type compose_type: str
    :returns: List of package names (name only, not NEVRA)
    :rtype: list

    Currently this is only needed by live-iso, it reads ./live/live-install.tmpl and
    processes only the installpkg lines. It lists the packages needed to complete creation of the
    iso using the templates such as x86.tmpl

    Keep in mind that the live-install.tmpl is shared between livemedia-creator and lorax-composer,
    even though the results are applied differently.
    """
    if compose_type != "live-iso":
        return []

    # get the arch information to pass to the runner
    arch = ArchData(get_buildarch(dbo))
    defaults = DataHolder(basearch=arch.basearch)
    templatedir = joinpaths(find_templates(share_dir), "live")
    runner = LiveTemplateRunner(dbo, templatedir=templatedir, defaults=defaults)
    runner.run("live-install.tmpl")
    log.debug("extra pkgs = %s", runner.pkgs)

    return runner.pkgnames


def start_build(cfg, dnflock, gitlock, branch, recipe_name, compose_type, test_mode=0):
    """ Start the build

    :param cfg: Configuration object
    :type cfg: ComposerConfig
    :param dnflock: Lock and YumBase for depsolving
    :type dnflock: YumLock
    :param recipe: The recipe to build
    :type recipe: str
    :param compose_type: The type of output to create from the recipe
    :type compose_type: str
    :returns: Unique ID for the build that can be used to track its status
    :rtype: str
    """
    share_dir = cfg.get("composer", "share_dir")
    lib_dir = cfg.get("composer", "lib_dir")

    # Make sure compose_type is valid, only allow enabled types
    type_enabled = dict(compose_types(share_dir)).get(compose_type)
    if type_enabled is None:
        raise RuntimeError("Invalid compose type (%s), must be one of %s" % (compose_type, [t for t, e in compose_types(share_dir)]))
    if not type_enabled:
        raise RuntimeError("Compose type '%s' is disabled on this architecture" % compose_type)

    # Some image types (live-iso) need extra packages for composer to execute the output template
    with dnflock.lock:
        extra_pkgs = get_extra_pkgs(dnflock.dbo, share_dir, compose_type)
    log.debug("Extra packages needed for %s: %s", compose_type, extra_pkgs)

    with gitlock.lock:
        (commit_id, recipe) = read_recipe_and_id(gitlock.repo, branch, recipe_name)

    # Combine modules and packages and depsolve the list
    module_nver = recipe.module_nver
    package_nver = recipe.package_nver
    package_nver.extend([(name, '*') for name in extra_pkgs])

    projects = sorted(set(module_nver+package_nver), key=lambda p: p[0].lower())
    deps = []
    log.info("depsolving %s", recipe["name"])
    try:
        # This can possibly update repodata and reset the YumBase object.
        with dnflock.lock_check:
            (installed_size, deps) = projects_depsolve_with_size(dnflock.dbo, projects, recipe.group_names, with_core=False)
    except ProjectsError as e:
        log.error("start_build depsolve: %s", str(e))
        raise RuntimeError("Problem depsolving %s: %s" % (recipe["name"], str(e)))

    # Read the kickstart template for this type
    ks_template_path = joinpaths(share_dir, "composer", compose_type) + ".ks"
    ks_template = open(ks_template_path, "r").read()

    # How much space will the packages in the default template take?
    ks_version = makeVersion()
    ks = KickstartParser(ks_version, errorsAreFatal=False, missingIncludeIsFatal=False)
    ks.readKickstartFromString(ks_template+"\n%end\n")
    pkgs = [(name, "*") for name in ks.handler.packages.packageList]
    grps = [grp.name for grp in ks.handler.packages.groupList]
    try:
        with dnflock.lock:
            (template_size, _) = projects_depsolve_with_size(dnflock.dbo, pkgs, grps, with_core=not ks.handler.packages.nocore)
    except ProjectsError as e:
        log.error("start_build depsolve: %s", str(e))
        raise RuntimeError("Problem depsolving %s: %s" % (recipe["name"], str(e)))
    log.debug("installed_size = %d, template_size=%d", installed_size, template_size)

    # Minimum LMC disk size is 1GiB, and anaconda bumps the estimated size up by 10% (which doesn't always work).
    installed_size = int((installed_size+template_size)) * 1.2
    log.debug("/ partition size = %d", installed_size)

    # Create the results directory
    build_id = str(uuid4())
    results_dir = joinpaths(lib_dir, "results", build_id)
    os.makedirs(results_dir)

    # Write the recipe commit hash
    commit_path = joinpaths(results_dir, "COMMIT")
    with open(commit_path, "w") as f:
        f.write(commit_id)

    # Write the original recipe
    recipe_path = joinpaths(results_dir, "blueprint.toml")
    with open(recipe_path, "w") as f:
        f.write(recipe.toml())

    # Write the frozen recipe
    frozen_recipe = recipe.freeze(deps)
    recipe_path = joinpaths(results_dir, "frozen.toml")
    with open(recipe_path, "w") as f:
        f.write(frozen_recipe.toml())

    # Write out the dependencies to the results dir
    deps_path = joinpaths(results_dir, "deps.toml")
    with open(deps_path, "w") as f:
        f.write(toml.dumps({"packages":deps}))

    # Save a copy of the original kickstart
    shutil.copy(ks_template_path, results_dir)

    with dnflock.lock:
        repos = list(dnflock.dbo.repos.iter_enabled())
    if not repos:
        raise RuntimeError("No enabled repos, canceling build.")

    # Create the git rpms, if any, and return the path to the repo under results_dir
    gitrpm_repo = create_gitrpm_repo(results_dir, recipe)

    # Create the final kickstart with repos and package list
    ks_path = joinpaths(results_dir, "final-kickstart.ks")
    with open(ks_path, "w") as f:
        ks_url = repo_to_ks(repos[0], "url")
        log.debug("url = %s", ks_url)
        f.write('url %s\n' % ks_url)
        for idx, r in enumerate(repos[1:]):
            ks_repo = repo_to_ks(r, "baseurl")
            log.debug("repo composer-%s = %s", idx, ks_repo)
            f.write('repo --name="composer-%s" %s\n' % (idx, ks_repo))

        if gitrpm_repo:
            log.debug("repo gitrpms = %s", gitrpm_repo)
            f.write('repo --name="gitrpms" --baseurl="file://%s"\n' % gitrpm_repo)

        # Setup the disk for booting
        # TODO Add GPT and UEFI boot support
        f.write('clearpart --all --initlabel\n')

        # Write the root partition and it's size in MB (rounded up)
        f.write('part / --size=%d\n' % ceil(installed_size / 1024**2))

        # Some customizations modify the template before writing it
        f.write(customize_ks_template(ks_template, recipe))

        for d in deps:
            f.write(dep_nevra(d)+"\n")

        # Include the rpms from the gitrpm repo directory
        if gitrpm_repo:
            for rpm in glob(os.path.join(gitrpm_repo, "*.rpm")):
                f.write(os.path.basename(rpm)[:-4]+"\n")

        f.write("%end\n")

        # Other customizations can be appended to the kickstart
        add_customizations(f, recipe)

    # Setup the config to pass to novirt_install
    log_dir = joinpaths(results_dir, "logs/")
    cfg_args = compose_args(compose_type)

    # Get the title, project, and release version from the host
    if not os.path.exists("/etc/os-release"):
        log.error("/etc/os-release is missing, cannot determine product or release version")
    os_release = flatconfig("/etc/os-release")

    log.debug("os_release = %s", dict(os_release.items()))

    cfg_args["title"] = os_release.get("PRETTY_NAME", "")
    cfg_args["project"] = os_release.get("NAME", "")
    cfg_args["releasever"] = os_release.get("VERSION_ID", "")
    cfg_args["volid"] = ""
    cfg_args["extra_boot_args"] = get_kernel_append(recipe)

    if "compression" not in cfg_args:
        cfg_args["compression"] = "xz"

    if "compress_args" not in cfg_args:
        cfg_args["compress_args"] = []

    cfg_args.update({
        "ks":               [ks_path],
        "logfile":          log_dir,
        "timeout":          60,                     # 60 minute timeout
    })
    with open(joinpaths(results_dir, "config.toml"), "w") as f:
        f.write(toml.dumps(cfg_args))

    # Set the initial status
    open(joinpaths(results_dir, "STATUS"), "w").write("WAITING")

    # Set the test mode, if requested
    if test_mode > 0:
        open(joinpaths(results_dir, "TEST"), "w").write("%s" % test_mode)

    write_timestamp(results_dir, TS_CREATED)
    log.info("Adding %s (%s %s) to compose queue", build_id, recipe["name"], compose_type)
    os.symlink(results_dir, joinpaths(lib_dir, "queue/new/", build_id))

    return build_id

# Supported output types
def compose_types(share_dir):
    r""" Returns a list of tuples of the supported output types, and their state

    The output types come from the kickstart names in /usr/share/lorax/composer/\*ks

    If they are disabled on the current arch their state is False. If enabled, it is True.
    eg. [("alibaba", False), ("ext4-filesystem", True), ...]
    """
    # These are compose types that are not supported on an architecture. eg. hyper-v on s390
    # If it is not listed, it is allowed
    disable_map = {
        "arm": ["alibaba", "ami", "google", "hyper-v", "vhd", "vmdk"],
        "armhfp": ["alibaba", "ami", "google", "hyper-v", "vhd", "vmdk"],
        "aarch64": ["alibaba", "google", "hyper-v", "vhd", "vmdk"],
        "ppc": ["alibaba", "ami", "google", "hyper-v", "vhd", "vmdk"],
        "ppc64": ["alibaba", "ami", "google", "hyper-v", "vhd", "vmdk"],
        "ppc64le": ["alibaba", "ami", "google", "hyper-v", "vhd", "vmdk"],
        "s390": ["alibaba", "ami", "google", "hyper-v", "vhd", "vmdk"],
        "s390x": ["alibaba", "ami", "google", "hyper-v", "vhd", "vmdk"],
    }

    all_types = sorted([os.path.basename(ks)[:-3] for ks in glob(joinpaths(share_dir, "composer/*.ks"))])
    arch_disabled = disable_map.get(os.uname().machine, [])

    return [(t, t not in arch_disabled) for t in all_types]

def compose_args(compose_type):
    """ Returns the settings to pass to novirt_install for the compose type

    :param compose_type: The type of compose to create, from `compose_types()`
    :type compose_type: str

    This will return a dict of options that match the ArgumentParser options for livemedia-creator.
    These are the ones the define the type of output, it's filename, etc.
    Other options will be filled in by `make_compose()`
    """
    _MAP = {"tar":              {"make_iso":                False,
                                 "make_disk":               False,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                True,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              default_image_name("xz", "root.tar"),
                                 "tar_disk_name":           None,
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "live-iso":         {"make_iso":                True,
                                 "make_disk":               False,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              "live.iso",
                                 "tar_disk_name":           None,
                                 "fs_label":                "Anaconda",     # Live booting may expect this to be 'Anaconda'
                                 "image_only":              False,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                 "iso_only":                True,
                                 "iso_name":                "live.iso",
                                },
            "partitioned-disk": {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              "disk.img",
                                 "tar_disk_name":           None,
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "qcow2":            {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              "qcow2",
                                 "qemu_args":               [],
                                 "image_name":              "disk.qcow2",
                                 "tar_disk_name":           None,
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "ext4-filesystem":  {"make_iso":                False,
                                 "make_disk":               False,
                                 "make_fsimage":            True,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              "filesystem.img",
                                 "tar_disk_name":           None,
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "ami":              {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              False,
                                 "qemu_args":               [],
                                 "image_name":              "disk.ami",
                                 "tar_disk_name":           None,
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "vhd":              {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              "vpc",
                                 "qemu_args":               ["-o", "subformat=fixed,force_size"],
                                 "image_name":              "disk.vhd",
                                 "tar_disk_name":           None,
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "vmdk":             {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              "vmdk",
                                 "qemu_args":               [],
                                 "image_name":              "disk.vmdk",
                                 "tar_disk_name":           None,
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "openstack":        {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              "qcow2",
                                 "qemu_args":               [],
                                 "image_name":              "disk.qcow2",
                                 "tar_disk_name":           None,
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "google":           {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           True,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        1024,
                                 "image_type":              False,          # False instead of None because of TOML
                                 "qemu_args":               [],
                                 "image_name":              "disk.tar.gz",
                                 "tar_disk_name":           "disk.raw",
                                 "compression":             "gzip",
                                 "compress_args":           ["-9"],
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            "alibaba":          {"make_iso":                False,
                                 "make_disk":               True,
                                 "make_fsimage":            False,
                                 "make_appliance":          False,
                                 "make_ami":                False,
                                 "make_tar":                False,
                                 "make_tar_disk":           False,
                                 "make_pxe_live":           False,
                                 "make_ostree_live":        False,
                                 "make_oci":                False,
                                 "make_vagrant":            False,
                                 "ostree":                  False,
                                 "live_rootfs_keep_size":   False,
                                 "live_rootfs_size":        0,
                                 "image_size_align":        0,
                                 "image_type":              "qcow2",
                                 "qemu_args":               [],
                                 "image_name":              "disk.qcow2",
                                 "tar_disk_name":           None,
                                 "fs_label":                "",
                                 "image_only":              True,
                                 "app_name":                None,
                                 "app_template":            None,
                                 "app_file":                None,
                                },
            }
    return _MAP[compose_type]

def move_compose_results(cfg, results_dir):
    """Move the final image to the results_dir and cleanup the unneeded compose files

    :param cfg: Build configuration
    :type cfg: DataHolder
    :param results_dir: Directory to put the results into
    :type results_dir: str
    """
    if cfg["make_tar"]:
        shutil.move(joinpaths(cfg["result_dir"], cfg["image_name"]), results_dir)
    elif cfg["make_iso"]:
        # Output from live iso is always a boot.iso under images/, move and rename it
        shutil.move(joinpaths(cfg["result_dir"], cfg["iso_name"]), joinpaths(results_dir, cfg["image_name"]))
    elif cfg["make_disk"] or cfg["make_fsimage"]:
        shutil.move(joinpaths(cfg["result_dir"], cfg["image_name"]), joinpaths(results_dir, cfg["image_name"]))


    # Cleanup the compose directory, but only if it looks like a compose directory
    if os.path.basename(cfg["result_dir"]) == "compose":
        shutil.rmtree(cfg["result_dir"])
    else:
        log.error("Incorrect compose directory, not cleaning up")
