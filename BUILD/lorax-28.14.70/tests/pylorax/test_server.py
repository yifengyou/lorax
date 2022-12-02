# -*- coding: UTF-8 -*-
#
# Copyright (C) 2017  Red Hat, Inc.
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
import os
from configparser import ConfigParser, NoOptionError
from contextlib import contextmanager
from glob import glob
from rpmfluff import SimpleRpmBuild, expectedArch
import shutil
import tempfile
import time
from threading import Lock
import unittest

from flask import json
import pytoml as toml
from ..lib import create_git_repo

from pylorax.api.config import configure, make_dnf_dirs, make_queue_dirs
from pylorax.api.errors import *                               # pylint: disable=wildcard-import
from pylorax.api.queue import start_queue_monitor
from pylorax.api.recipes import open_or_create_repo, commit_recipe_directory
from pylorax.api.server import server, GitLock
from pylorax.api.dnfbase import DNFLock
from pylorax.sysutils import joinpaths

# Used for testing UTF-8 input support
UTF8_TEST_STRING = "I ｗ𝒊ll 𝟉ο𝘁 𝛠ａ𝔰ꜱ 𝘁𝒉𝝸𝚜"


# HELPER CONSTANTS.
HTTP_GLOB = {"name":"httpd", "version":"2.4.*"}
MODSSL_GLOB = {"name":"mod_ssl", "version":"2.4.*"}
PHP_GLOB = {"name":"php", "version":"7.*"}
PHPMYSQL_GLOB = {"name": "php-mysqlnd", "version":"7.*"}
OPENSSH_GLOB = {"name":"openssh-server", "version": "*"}
RSYNC_GLOB = {"name": "rsync", "version": "3.1.*"}
SAMBA_GLOB = {"name": "samba", "version": "4.*.*"}
TMUX_GLOB = {"name": "tmux", "version": "*"}
GLUSTERFS_GLOB = {"name": "glusterfs", "version": "*"}
GLUSTERFSFUSE_GLOB = {"name": "glusterfs-fuse", "version": "*"}

def get_system_repo():
    """Get an enabled system repo from /etc/yum.repos.d/*repo

    This will be used for test_projects_source_01_delete_system()
    """
    # The sources delete test needs the name of a system repo, get it from /etc/yum.repos.d/
    for sys_repo in sorted(glob("/etc/yum.repos.d/*repo")):
        cfg = ConfigParser()
        cfg.read(sys_repo)
        for section in cfg.sections():
            try:
                if cfg.get(section, "enabled") == "1":
                    return section
            except NoOptionError:
                pass

    # Failed to find one, fall back to using base
    return "base"

def docs_path():
    """Helper that points to where documentation should be.

    They may not be installed, so in that case the doc test should be skipped
    """
    try:
        return os.path.abspath(joinpaths(os.path.dirname(__file__), "../../../docs/html"))
    except IndexError:
        return "/usr/share/doc/lorax/html"

def _wait_for_status(self, uuid, wait_status):
    """Helper function that waits for a status

    :param uuid: UUID of the build to check
    :type uuid: str
    :param wait_status: List of statuses to exit on
    :type wait_status: list of str
    :returns: True if status was found, False if it timed out
    :rtype: bool

    This will time out after 60 seconds
    """
    start = time.time()
    while True:
        resp = self.server.get("/api/v0/compose/info/%s" % uuid)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        queue_status = data.get("queue_status")
        if queue_status in wait_status:
            return True
        if time.time() > start + 60:
            return False
        time.sleep(1)

class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(self):
        self.rawhide = False
        self.maxDiff = None

        repo_dir = tempfile.mkdtemp(prefix="lorax.test.repo.")
        server.config["REPO_DIR"] = repo_dir
        repo = open_or_create_repo(server.config["REPO_DIR"])
        server.config["GITLOCK"] = GitLock(repo=repo, lock=Lock(), dir=repo_dir)

        server.config["COMPOSER_CFG"] = configure(root_dir=repo_dir, test_config=True)
        os.makedirs(joinpaths(server.config["COMPOSER_CFG"].get("composer", "share_dir"), "composer"))
        errors = make_queue_dirs(server.config["COMPOSER_CFG"], 0)
        if errors:
            raise RuntimeError("\n".join(errors))

        make_dnf_dirs(server.config["COMPOSER_CFG"])

        # copy over the test_server dnf repositories
        dnf_repo_dir = server.config["COMPOSER_CFG"].get("composer", "repo_dir")
        for f in glob("./tests/pylorax/repos/server-*.repo"):
            shutil.copy2(f, dnf_repo_dir)

        # Modify fedora vs. rawhide tests when running on rawhide
        if os.path.exists("/etc/yum.repos.d/fedora-rawhide.repo"):
            self.rawhide = True

        # dnf repo baseurl has to point to an absolute directory, so we use /tmp/lorax-empty-repo/ in the files
        # and create an empty repository
        os.makedirs("/tmp/lorax-empty-repo/")
        os.system("createrepo_c /tmp/lorax-empty-repo/")

        server.config["DNFLOCK"] = DNFLock(server.config["COMPOSER_CFG"])

        # Include a message in /api/status output
        server.config["TEMPLATE_ERRORS"] = ["Test message"]

        server.config['TESTING'] = True
        self.server = server.test_client()
        self.repo_dir = repo_dir

        self.examples_path = "./tests/pylorax/blueprints/"

        # Copy the shared files over to the directory tree we are using
        share_path = "./share/composer/"
        for f in glob(joinpaths(share_path, "*")):
            shutil.copy(f, joinpaths(server.config["COMPOSER_CFG"].get("composer", "share_dir"), "composer"))

        # Import the example blueprints
        commit_recipe_directory(server.config["GITLOCK"].repo, "master", self.examples_path)

        # The sources delete test needs the name of a system repo, get it from /etc/yum.repos.d/
        self.system_repo = get_system_repo()

        start_queue_monitor(server.config["COMPOSER_CFG"], 0, 0)

    @classmethod
    def tearDownClass(self):
        shutil.rmtree(server.config["REPO_DIR"])
        shutil.rmtree("/tmp/lorax-empty-repo/")

    def test_01_status(self):
        """Test the /api/status route"""
        status_fields = ["build", "api", "db_version", "schema_version", "db_supported", "backend", "msgs"]
        resp = self.server.get("/api/status")
        data = json.loads(resp.data)
        # Make sure the fields are present
        self.assertEqual(sorted(data.keys()), sorted(status_fields))

        # Check for test message
        self.assertEqual(data["msgs"], ["Test message"])


    def test_02_blueprints_list(self):
        """Test the /api/v0/blueprints/list route"""
        list_dict = {"blueprints":["example-append", "example-atlas", "example-custom-base", "example-development",
                                   "example-glusterfs", "example-http-server", "example-jboss",
                                   "example-kubernetes"], "limit":20, "offset":0, "total":8}
        resp = self.server.get("/api/v0/blueprints/list")
        data = json.loads(resp.data)
        self.assertEqual(data, list_dict)

        # Make sure limit=0 still returns the correct total
        resp = self.server.get("/api/v0/blueprints/list?limit=0")
        data = json.loads(resp.data)
        self.assertEqual(data["limit"], 0)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["total"], list_dict["total"])

    def test_03_blueprints_info_1(self):
        """Test the /api/v0/blueprints/info route with one blueprint"""
        info_dict_1 = {"changes":[{"changed":False, "name":"example-http-server"}],
                       "errors":[],
                       "blueprints":[{"description":"An example http server with PHP and MySQL support.",
                                   "modules":[HTTP_GLOB,
                                              MODSSL_GLOB,
                                              PHP_GLOB,
                                              PHPMYSQL_GLOB],
                                   "name":"example-http-server",
                                   "packages": [OPENSSH_GLOB,
                                                RSYNC_GLOB,
                                                TMUX_GLOB],
                                   "groups": [],
                                   "version": "0.0.1"}]}
        resp = self.server.get("/api/v0/blueprints/info/example-http-server")
        data = json.loads(resp.data)
        self.assertEqual(data, info_dict_1)

    def test_03_blueprints_info_2(self):
        """Test the /api/v0/blueprints/info route with 2 blueprints"""
        info_dict_2 = {"changes":[{"changed":False, "name":"example-glusterfs"},
                                  {"changed":False, "name":"example-http-server"}],
                       "errors":[],
                       "blueprints":[{"description": "An example GlusterFS server with samba",
                                   "modules":[GLUSTERFS_GLOB,
                                              GLUSTERFSFUSE_GLOB],
                                   "name":"example-glusterfs",
                                   "packages":[SAMBA_GLOB],
                                   "groups": [],
                                   "version": "0.0.1"},
                                  {"description":"An example http server with PHP and MySQL support.",
                                   "modules":[HTTP_GLOB,
                                              MODSSL_GLOB,
                                              PHP_GLOB,
                                              PHPMYSQL_GLOB],
                                   "name":"example-http-server",
                                   "packages": [OPENSSH_GLOB,
                                                RSYNC_GLOB,
                                                TMUX_GLOB],
                                   "groups": [],
                                   "version": "0.0.1"},
                                 ]}
        resp = self.server.get("/api/v0/blueprints/info/example-http-server,example-glusterfs")
        data = json.loads(resp.data)
        self.assertEqual(data, info_dict_2)

    def test_03_blueprints_info_none(self):
        """Test the /api/v0/blueprints/info route with an unknown blueprint"""
        resp = self.server.get("/api/v0/blueprints/info/missing-blueprint")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue(len(data["errors"]) > 0)
        self.assertEqual(data["errors"][0]["id"], "UnknownBlueprint")

    def test_04_blueprints_changes(self):
        """Test the /api/v0/blueprints/changes route"""
        resp = self.server.get("/api/v0/blueprints/changes/example-http-server")
        data = json.loads(resp.data)

        # Can't compare a whole dict since commit hash and timestamps will change.
        # Should have 1 commit (for now), with a matching message.
        self.assertEqual(data["limit"], 20)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(len(data["errors"]), 0)
        self.assertEqual(len(data["blueprints"]), 1)
        self.assertEqual(data["blueprints"][0]["name"], "example-http-server")
        self.assertEqual(len(data["blueprints"][0]["changes"]), 1)

        # Make sure limit=0 still returns the correct total
        resp = self.server.get("/api/v0/blueprints/changes/example-http-server?limit=0")
        data = json.loads(resp.data)
        self.assertEqual(data["limit"], 0)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["blueprints"][0]["total"], 1)

    def test_04a_blueprints_diff_empty_ws(self):
        """Test the /api/v0/diff/NEWEST/WORKSPACE with empty workspace"""
        resp = self.server.get("/api/v0/blueprints/diff/example-glusterfs/NEWEST/WORKSPACE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data, {"diff": []})

    def test_05_blueprints_new_json(self):
        """Test the /api/v0/blueprints/new route with json blueprint"""
        test_blueprint = {"description": "An example GlusterFS server with samba",
                       "name":"example-glusterfs",
                       "version": "0.2.0",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB],
                       "groups": []}

        resp = self.server.post("/api/v0/blueprints/new",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/info/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0], test_blueprint)

    def test_06_blueprints_new_toml(self):
        """Test the /api/v0/blueprints/new route with toml blueprint"""
        test_blueprint = open(joinpaths(self.examples_path, "example-glusterfs.toml"), "rb").read()
        resp = self.server.post("/api/v0/blueprints/new",
                                data=test_blueprint,
                                content_type="text/x-toml")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/info/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertEqual(len(blueprints), 1)

        # Returned blueprint has had its version bumped
        test_blueprint = toml.loads(test_blueprint)
        test_blueprint["version"] = "0.2.1"

        # The test_blueprint generated by toml.loads will not have any groups property
        # defined, since there are no groups listed.  However, /api/v0/blueprints/new will
        # return an object with groups=[].  So, add that here to keep the equality test
        # working.
        test_blueprint["groups"] = []

        self.assertEqual(blueprints[0], test_blueprint)

    def test_07_blueprints_ws_json(self):
        """Test the /api/v0/blueprints/workspace route with json blueprint"""
        test_blueprint = {"description": "An example GlusterFS server with samba, ws version",
                       "name":"example-glusterfs",
                       "version": "0.3.0",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB],
                       "groups": []}

        resp = self.server.post("/api/v0/blueprints/workspace",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/info/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0], test_blueprint)
        changes = data.get("changes")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0], {"name":"example-glusterfs", "changed":True})

    def test_08_blueprints_ws_toml(self):
        """Test the /api/v0/blueprints/workspace route with toml blueprint"""
        test_blueprint = {"description": "An example GlusterFS server with samba, ws version",
                       "name":"example-glusterfs",
                       "version": "0.4.0",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB],
                       "groups": []}

        resp = self.server.post("/api/v0/blueprints/workspace",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/info/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0], test_blueprint)
        changes = data.get("changes")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0], {"name":"example-glusterfs", "changed":True})

    def test_09_blueprints_ws_delete(self):
        """Test DELETE /api/v0/blueprints/workspace/<blueprint_name>"""
        # Write to the workspace first, just use the test_blueprints_ws_json test for this
        self.test_07_blueprints_ws_json()

        # Delete it
        resp = self.server.delete("/api/v0/blueprints/workspace/example-glusterfs")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        # Make sure it isn't the workspace copy and that changed is False
        resp = self.server.get("/api/v0/blueprints/info/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0]["version"], "0.2.1")
        changes = data.get("changes")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0], {"name":"example-glusterfs", "changed":False})

    def test_10_blueprints_delete(self):
        """Test DELETE /api/v0/blueprints/delete/<blueprint_name>"""

        # Push a new workspace blueprint first
        test_blueprint = {"description": "An example GlusterFS server with samba, ws version",
                       "name":"example-glusterfs",
                       "version": "1.4.0",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB],
                       "groups": []}
        resp = self.server.post("/api/v0/blueprints/workspace",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})
        # Make sure the workspace file is present
        self.assertEqual(os.path.exists(joinpaths(self.repo_dir, "git/workspace/master/example-glusterfs.toml")), True)

        # This should delete the git blueprint and the workspace copy
        resp = self.server.delete("/api/v0/blueprints/delete/example-glusterfs")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        # Make sure example-glusterfs is no longer in the list of blueprints
        resp = self.server.get("/api/v0/blueprints/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertEqual("example-glusterfs" in blueprints, False)

        # Make sure the workspace file is gone
        self.assertEqual(os.path.exists(joinpaths(self.repo_dir, "git/workspace/master/example-glusterfs.toml")), False)

    # This has to run after the above test
    def test_10_blueprints_delete_2(self):
        """Test running a compose with the deleted blueprint"""
        # Trying to start a compose with a deleted blueprint should fail
        test_compose = {"blueprint_name": "example-glusterfs",
                        "compose_type": "tar",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=2",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False, "Compose of deleted blueprint did not fail: %s" % data)

    def test_11_blueprints_undo(self):
        """Test POST /api/v0/blueprints/undo/<blueprint_name>/<commit>"""
        resp = self.server.get("/api/v0/blueprints/changes/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)

        # Revert it to the first commit
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        changes = blueprints[0].get("changes")
        self.assertEqual(len(changes) > 1, True)

        # Revert it to the first commit
        commit = changes[-1]["commit"]
        resp = self.server.post("/api/v0/blueprints/undo/example-glusterfs/%s" % commit)
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/changes/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)

        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        changes = blueprints[0].get("changes")
        self.assertEqual(len(changes) > 1, True)

        expected_msg = "example-glusterfs.toml reverted to commit %s" % commit
        self.assertEqual(changes[0]["message"], expected_msg)

    def test_12_blueprints_tag(self):
        """Test POST /api/v0/blueprints/tag/<blueprint_name>"""
        resp = self.server.post("/api/v0/blueprints/tag/example-glusterfs")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/changes/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)

        # Revert it to the first commit
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        changes = blueprints[0].get("changes")
        self.assertEqual(len(changes) > 1, True)
        self.assertEqual(changes[0]["revision"], 1)

    def test_13_blueprints_diff(self):
        """Test /api/v0/blueprints/diff/<blueprint_name>/<from_commit>/<to_commit>"""
        resp = self.server.get("/api/v0/blueprints/changes/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        changes = blueprints[0].get("changes")
        self.assertEqual(len(changes) >= 2, True)

        from_commit = changes[1].get("commit")
        self.assertNotEqual(from_commit, None)
        to_commit = changes[0].get("commit")
        self.assertNotEqual(to_commit, None)

        print("from: %s" % from_commit)
        print("to: %s" % to_commit)
        print(changes)

        # Get the differences between the two commits
        resp = self.server.get("/api/v0/blueprints/diff/example-glusterfs/%s/%s" % (from_commit, to_commit))
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data, {"diff": [{"new": {"Version": "0.0.1"}, "old": {"Version": "0.2.1"}}]})

        # Write to the workspace and check the diff
        test_blueprint = {"description": "An example GlusterFS server with samba, ws version",
                       "name":"example-glusterfs",
                       "version": "0.3.0",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB]}

        resp = self.server.post("/api/v0/blueprints/workspace",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        # Get the differences between the newest commit and the workspace
        resp = self.server.get("/api/v0/blueprints/diff/example-glusterfs/NEWEST/WORKSPACE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        result = {"diff": [{"new": {"Description": "An example GlusterFS server with samba, ws version"},
                             "old": {"Description": "An example GlusterFS server with samba"}},
                            {"new": {"Version": "0.3.0"},
                             "old": {"Version": "0.0.1"}},
                            {"new": {"Package": TMUX_GLOB},
                             "old": None}]}
        self.assertEqual(data, result)

    def test_14_blueprints_depsolve(self):
        """Test /api/v0/blueprints/depsolve/<blueprint_names>"""
        resp = self.server.get("/api/v0/blueprints/depsolve/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0]["blueprint"]["name"], "example-glusterfs")
        self.assertEqual(len(blueprints[0]["dependencies"]) > 10, True)
        self.assertFalse(data.get("errors"))

    def test_14_blueprints_depsolve_empty(self):
        """Test /api/v0/blueprints/depsolve/<blueprint_names> on empty blueprint"""
        test_blueprint = {"description": "An empty blueprint",
                       "name":"void",
                       "version": "0.1.0"}
        resp = self.server.post("/api/v0/blueprints/new",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/depsolve/void")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0]["blueprint"]["name"], "void")
        self.assertEqual(blueprints[0]["blueprint"]["packages"], [])
        self.assertEqual(blueprints[0]["blueprint"]["modules"], [])
        self.assertEqual(blueprints[0]["dependencies"], [])
        self.assertFalse(data.get("errors"))

    def test_15_blueprints_freeze(self):
        """Test /api/v0/blueprints/freeze/<blueprint_names>"""
        resp = self.server.get("/api/v0/blueprints/freeze/example-glusterfs")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        self.assertEqual(len(blueprints), 1)
        self.assertTrue(len(blueprints[0]["blueprint"]["modules"]) > 0)
        self.assertEqual(blueprints[0]["blueprint"]["name"], "example-glusterfs")
        evra = blueprints[0]["blueprint"]["modules"][0]["version"]
        self.assertEqual(len(evra) > 10, True)

    def test_projects_list(self):
        """Test /api/v0/projects/list"""
        resp = self.server.get("/api/v0/projects/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        projects = data.get("projects")
        self.assertEqual(len(projects) > 10, True)

        expected_total = data["total"]

        # Make sure limit=0 still returns the correct total
        resp = self.server.get("/api/v0/projects/list?limit=0")
        data = json.loads(resp.data)
        self.assertEqual(data["total"], expected_total)

    def test_projects_info(self):
        """Test /api/v0/projects/info/<project_names>"""
        resp = self.server.get("/api/v0/projects/info/bash")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        projects = data.get("projects")
        self.assertEqual(len(projects) > 0, True)
        self.assertEqual(projects[0]["name"], "bash")
        self.assertEqual(projects[0]["builds"][0]["source"]["license"], "GPLv3+")

    def test_projects_depsolve(self):
        """Test /api/v0/projects/depsolve/<project_names>"""
        resp = self.server.get("/api/v0/projects/depsolve/bash")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        deps = data.get("projects")
        self.assertEqual(len(deps) > 10, True)
        self.assertTrue("basesystem" in [dep["name"] for dep in deps])

    def test_projects_source_00_list(self):
        """Test /api/v0/projects/source/list"""
        resp = self.server.get("/api/v0/projects/source/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        # Make sure it lists some common sources
        for r in ["lorax-1", "lorax-2", "lorax-3", "lorax-4", "other-repo", "single-repo"]:
            self.assertTrue(r in data["sources"] )

    def test_projects_source_00_info(self):
        """Test /api/v0/projects/source/info"""
        resp = self.server.get("/api/v0/projects/source/info/single-repo")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        sources = data["sources"]
        self.assertTrue("single-repo" in sources)

    def test_projects_source_00_new_json(self):
        """Test /api/v0/projects/source/new with a new json source"""
        json_source = open("./tests/pylorax/source/test-repo.json").read()
        self.assertTrue(len(json_source) > 0)
        resp = self.server.post("/api/v0/projects/source/new",
                                data=json_source,
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        # Is it listed?
        resp = self.server.get("/api/v0/projects/source/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        sources = data["sources"]
        self.assertTrue("new-repo-1" in sources)

    def test_projects_source_00_new_toml(self):
        """Test /api/v0/projects/source/new with a new toml source"""
        toml_source = open("./tests/pylorax/source/test-repo.toml").read()
        self.assertTrue(len(toml_source) > 0)
        resp = self.server.post("/api/v0/projects/source/new",
                                data=toml_source,
                                content_type="text/x-toml")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        # Is it listed?
        resp = self.server.get("/api/v0/projects/source/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        sources = data["sources"]
        self.assertTrue("new-repo-2" in sources)

    def test_projects_source_00_replace(self):
        """Test /api/v0/projects/source/new with a replacement source"""
        toml_source = open("./tests/pylorax/source/replace-repo.toml").read()
        self.assertTrue(len(toml_source) > 0)
        resp = self.server.post("/api/v0/projects/source/new",
                                data=toml_source,
                                content_type="text/x-toml")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        # Check to see if it was really changed
        resp = self.server.get("/api/v0/projects/source/info/single-repo")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        sources = data["sources"]
        self.assertTrue("single-repo" in sources)
        repo = sources["single-repo"]
        self.assertEqual(repo["check_ssl"], False)
        self.assertTrue("gpgkey_urls" not in repo)

    def test_projects_source_00_bad_url(self):
        """Test /api/v0/projects/source/new with a new source that has an invalid url"""
        toml_source = open("./tests/pylorax/source/bad-repo.toml").read()
        self.assertTrue(len(toml_source) > 0)
        resp = self.server.post("/api/v0/projects/source/new",
                                data=toml_source,
                                content_type="text/x-toml")
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], False)

    def test_projects_source_01_delete_system(self):
        """Test /api/v0/projects/source/delete a system source"""
        if self.rawhide:
            resp = self.server.delete("/api/v0/projects/source/delete/rawhide")
        else:
            resp = self.server.delete("/api/v0/projects/source/delete/fedora")
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False)

        # Make sure fedora/rawhide is still listed
        resp = self.server.get("/api/v0/projects/source/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue(self.system_repo in data["sources"], "%s not in %s" % (self.system_repo, data["sources"]))

    def test_projects_source_02_delete_single(self):
        """Test /api/v0/projects/source/delete a single source"""
        resp = self.server.delete("/api/v0/projects/source/delete/single-repo")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data, {"status":True})

        # Make sure single-repo isn't listed
        resp = self.server.get("/api/v0/projects/source/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue("single-repo" not in data["sources"])

    def test_projects_source_03_delete_unknown(self):
        """Test /api/v0/projects/source/delete an unknown source"""
        resp = self.server.delete("/api/v0/projects/source/delete/unknown-repo")
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False)

    def test_projects_source_04_delete_multi(self):
        """Test /api/v0/projects/source/delete a source from a file with multiple sources"""
        resp = self.server.delete("/api/v0/projects/source/delete/lorax-3")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data, {"status":True})

        # Make sure single-repo isn't listed
        resp = self.server.get("/api/v0/projects/source/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue("lorax-3" not in data["sources"])

    def test_modules_list(self):
        """Test /api/v0/modules/list"""
        resp = self.server.get("/api/v0/modules/list")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        modules = data.get("modules")
        self.assertEqual(len(modules) > 10, True)
        self.assertEqual(modules[0]["group_type"], "rpm")

        expected_total = data["total"]

        resp = self.server.get("/api/v0/modules/list/d*")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        modules = data.get("modules")
        self.assertEqual(len(modules) > 0, True)
        self.assertEqual(modules[0]["name"].startswith("d"), True)

        # Make sure limit=0 still returns the correct total
        resp = self.server.get("/api/v0/modules/list?limit=0")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["total"], expected_total)

    def test_modules_info(self):
        """Test /api/v0/modules/info"""
        resp = self.server.get("/api/v0/modules/info/bash")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        modules = data.get("modules")
        self.assertEqual(len(modules) > 0, True)
        self.assertEqual(modules[0]["name"], "bash")
        self.assertTrue("basesystem" in [dep["name"] for dep in modules[0]["dependencies"]])

    def test_blueprint_new_branch(self):
        """Test the /api/v0/blueprints/new route with a new branch"""
        test_blueprint = {"description": "An example GlusterFS server with samba",
                       "name":"example-glusterfs",
                       "version": "0.2.0",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB],
                       "groups": []}

        resp = self.server.post("/api/v0/blueprints/new?branch=test",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/info/example-glusterfs?branch=test")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0], test_blueprint)

    def assert_documentation(self, response):
        """
            Assert response containing documentation from /api/doc/ is
            valid *without* comparing to the actual file on disk.
        """
        self.assertEqual(200, response.status_code)
        self.assertTrue(len(response.data) > 1024)
        # look for some well known strings inside the documentation
        self.assertRegex(response.data.decode("utf-8"), r"Lorax [\d.]+ documentation")
        self.assertRegex(response.data.decode("utf-8"), r"Copyright \d+, Red Hat, Inc.")

    @unittest.skipUnless(os.path.exists(docs_path()), "Test requires a running API server")
    def test_api_docs(self):
        """Test the /api/docs/"""
        resp = self.server.get("/api/docs/")
        self.assert_documentation(resp)

    @unittest.skipUnless(os.path.exists(docs_path()), "Test requires a running API server")
    def test_api_docs_with_existing_path(self):
        """Test the /api/docs/modules.html"""
        resp = self.server.get("/api/docs/modules.html")
        self.assert_documentation(resp)

    def test_compose_01_types(self):
        """Test the /api/v0/compose/types route"""
        resp = self.server.get("/api/v0/compose/types")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual({"name": "tar", "enabled": True} in data["types"], True)

        # All of the non-x86 compose types disable alibaba
        if os.uname().machine != 'x86_64':
            self.assertEqual({"name": "alibaba", "enabled": False} in data["types"], True)

    def test_compose_02_bad_type(self):
        """Test that using an unsupported image type failes"""
        test_compose = {"blueprint_name": "example-glusterfs",
                        "compose_type": "snakes",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=1",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False, "Failed to fail to start test compose: %s" % data)
        self.assertEqual(data["errors"], [{"id": BAD_COMPOSE_TYPE, "msg": "Invalid compose type (snakes), must be one of ['alibaba', 'ami', 'ext4-filesystem', 'google', 'live-iso', 'openstack', 'partitioned-disk', 'qcow2', 'tar', 'vhd', 'vmdk']"}],
                                         "Failed to get errors: %s" % data)

    def test_compose_03_status_fail(self):
        """Test that requesting a status for a bad uuid is empty"""
        resp = self.server.get("/api/v0/compose/status/NO-UUID-TO-SEE-HERE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["uuids"], [], "Failed to get empty result bad uuid: %s" % data)

    def test_compose_04_cancel_fail(self):
        """Test that requesting a cancel for a bad uuid fails."""
        resp = self.server.delete("/api/v0/compose/cancel/NO-UUID-TO-SEE-HERE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False, "Failed to get an error for a bad uuid: %s" % data)
        self.assertEqual(data["errors"], [{"id": UNKNOWN_UUID, "msg": "NO-UUID-TO-SEE-HERE is not a valid build uuid"}],
                                          "Failed to get errors: %s" % data)

    def test_compose_05_delete_fail(self):
        """Test that requesting a delete for a bad uuid fails."""
        resp = self.server.delete("/api/v0/compose/delete/NO-UUID-TO-SEE-HERE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["errors"], [{"id": UNKNOWN_UUID, "msg": "no-uuid-to-see-here is not a valid build uuid"}],
                         "Failed to get an error for a bad uuid: %s" % data)

    def test_compose_06_info_fail(self):
        """Test that requesting info for a bad uuid fails."""
        resp = self.server.get("/api/v0/compose/info/NO-UUID-TO-SEE-HERE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False, "Failed to get an error for a bad uuid: %s" % data)
        self.assertEqual(data["errors"], [{"id": UNKNOWN_UUID, "msg": "NO-UUID-TO-SEE-HERE is not a valid build uuid"}],
                                         "Failed to get errors: %s" % data)

    def test_compose_07_metadata_fail(self):
        """Test that requesting metadata for a bad uuid fails."""
        resp = self.server.get("/api/v0/compose/metadata/NO-UUID-TO-SEE-HERE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False, "Failed to get an error for a bad uuid: %s" % data)
        self.assertEqual(data["errors"], [{"id": UNKNOWN_UUID, "msg": "NO-UUID-TO-SEE-HERE is not a valid build uuid"}],
                                         "Failed to get errors: %s" % data)

    def test_compose_08_results_fail(self):
        """Test that requesting results for a bad uuid fails."""
        resp = self.server.get("/api/v0/compose/results/NO-UUID-TO-SEE-HERE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False, "Failed to get an error for a bad uuid: %s" % data)
        self.assertEqual(data["errors"], [{"id": UNKNOWN_UUID, "msg": "NO-UUID-TO-SEE-HERE is not a valid build uuid"}],
                                          "Failed to get errors: %s" % data)

    def test_compose_09_logs_fail(self):
        """Test that requesting logs for a bad uuid fails."""
        resp = self.server.get("/api/v0/compose/logs/NO-UUID-TO-SEE-HERE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False, "Failed to get an error for a bad uuid: %s" % data)
        self.assertEqual(data["errors"], [{"id": UNKNOWN_UUID, "msg": "NO-UUID-TO-SEE-HERE is not a valid build uuid"}],
                                         "Failed to get errors: %s" % data)

    def test_compose_10_log_fail(self):
        """Test that requesting log for a bad uuid fails."""
        resp = self.server.get("/api/v0/compose/log/NO-UUID-TO-SEE-HERE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False, "Failed to get an error for a bad uuid: %s" % data)
        self.assertEqual(data["errors"], [{"id": UNKNOWN_UUID, "msg": "NO-UUID-TO-SEE-HERE is not a valid build uuid"}],
                                         "Failed to get errors: %s" % data)

    def test_compose_11_create_failed(self):
        """Test the /api/v0/compose routes with a failed test compose"""
        test_compose = {"blueprint_name": "example-glusterfs",
                        "compose_type": "tar",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=1",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to start test compose: %s" % data)

        build_id = data["build_id"]

        # Is it in the queue list (either new or run is fine, based on timing)
        resp = self.server.get("/api/v0/compose/queue")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["new"] + data["run"]]
        self.assertEqual(build_id in ids, True, "Failed to add build to the queue")

        # Wait for it to start
        self.assertEqual(_wait_for_status(self, build_id, ["RUNNING"]), True, "Failed to start test compose")

        # Wait for it to finish
        self.assertEqual(_wait_for_status(self, build_id, ["FAILED"]), True, "Failed to finish test compose")

        resp = self.server.get("/api/v0/compose/info/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["queue_status"], "FAILED", "Build not in FAILED state")

        # Test the /api/v0/compose/failed route
        resp = self.server.get("/api/v0/compose/failed")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["failed"]]
        self.assertEqual(build_id in ids, True, "Failed build not listed by /compose/failed")

        # Test the /api/v0/compose/finished route
        resp = self.server.get("/api/v0/compose/finished")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["finished"], [], "Finished build not listed by /compose/finished")

        # Test the /api/v0/compose/status/<uuid> route
        resp = self.server.get("/api/v0/compose/status/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [(e["id"], e["queue_status"]) for e in data["uuids"]]
        self.assertEqual((build_id, "FAILED") in ids, True, "Failed build not listed by /compose/status")

        # Test the /api/v0/compose/cancel/<uuid> route
        resp = self.server.post("/api/v0/compose?test=1",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to start test compose: %s" % data)

        cancel_id = data["build_id"]

        # Wait for it to start
        self.assertEqual(_wait_for_status(self, cancel_id, ["RUNNING"]), True, "Failed to start test compose")

        # Cancel the build
        resp = self.server.delete("/api/v0/compose/cancel/%s" % cancel_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to cancel test compose: %s" % data)

        # Delete the failed build
        # Test the /api/v0/compose/delete/<uuid> route
        resp = self.server.delete("/api/v0/compose/delete/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [(e["uuid"], e["status"]) for e in data["uuids"]]
        self.assertEqual((build_id, True) in ids, True, "Failed to delete test compose: %s" % data)

        # Make sure the failed list is empty
        resp = self.server.get("/api/v0/compose/failed")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["failed"], [], "Failed to delete the failed build: %s" % data)

    def test_compose_12_create_finished(self):
        """Test the /api/v0/compose routes with a finished test compose"""
        test_compose = {"blueprint_name": "example-custom-base",
                        "compose_type": "tar",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=2",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to start test compose: %s" % data)

        build_id = data["build_id"]

        # Is it in the queue list (either new or run is fine, based on timing)
        resp = self.server.get("/api/v0/compose/queue")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["new"] + data["run"]]
        self.assertEqual(build_id in ids, True, "Failed to add build to the queue")

        # Wait for it to start
        self.assertEqual(_wait_for_status(self, build_id, ["RUNNING"]), True, "Failed to start test compose")

        # Wait for it to finish
        self.assertEqual(_wait_for_status(self, build_id, ["FINISHED"]), True, "Failed to finish test compose")

        resp = self.server.get("/api/v0/compose/info/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["queue_status"], "FINISHED", "Build not in FINISHED state")

        # Test the /api/v0/compose/finished route
        resp = self.server.get("/api/v0/compose/finished")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["finished"]]
        self.assertEqual(build_id in ids, True, "Finished build not listed by /compose/finished")

        # Test the /api/v0/compose/failed route
        resp = self.server.get("/api/v0/compose/failed")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["failed"], [], "Failed build not listed by /compose/failed")

        # Test the /api/v0/compose/status/<uuid> route
        resp = self.server.get("/api/v0/compose/status/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [(e["id"], e["queue_status"]) for e in data["uuids"]]
        self.assertEqual((build_id, "FINISHED") in ids, True, "Finished build not listed by /compose/status")

        # Test the /api/v0/compose/metadata/<uuid> route
        resp = self.server.get("/api/v0/compose/metadata/%s" % build_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data) > 1024, True)

        # Test the /api/v0/compose/results/<uuid> route
        resp = self.server.get("/api/v0/compose/results/%s" % build_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data) > 1024, True)

        # Test the /api/v0/compose/image/<uuid> route
        resp = self.server.get("/api/v0/compose/image/%s" % build_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data) > 0, True)
        self.assertEqual(resp.data, b"TEST IMAGE")

        # Examine the final-kickstart.ks for the customizations
        # A bit kludgy since it examines the filesystem directly, but that's better than unpacking the metadata
        final_ks = open(joinpaths(self.repo_dir, "var/lib/lorax/composer/results/", build_id, "final-kickstart.ks")).read()

        # Check for the expected customizations in the kickstart
        self.assertTrue("network --hostname=" in final_ks)
        self.assertTrue("sshkey --user root" in final_ks)

        # Examine the config.toml to make sure it has an empty extra_boot_args
        cfg_path = joinpaths(self.repo_dir, "var/lib/lorax/composer/results/", build_id, "config.toml")
        cfg_dict = toml.loads(open(cfg_path, "r").read())
        self.assertTrue("extra_boot_args" in cfg_dict)
        self.assertEqual(cfg_dict["extra_boot_args"], "")

        # Delete the finished build
        # Test the /api/v0/compose/delete/<uuid> route
        resp = self.server.delete("/api/v0/compose/delete/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [(e["uuid"], e["status"]) for e in data["uuids"]]
        self.assertEqual((build_id, True) in ids, True, "Failed to delete test compose: %s" % data)

        # Make sure the finished list is empty
        resp = self.server.get("/api/v0/compose/finished")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["finished"], [], "Failed to delete the failed build: %s" % data)

    def test_compose_13_status_filter(self):
        """Test filter arguments on the /api/v0/compose/status route"""
        # Get a couple compose results going so we have something to filter
        test_compose_fail = {"blueprint_name": "example-glusterfs",
                             "compose_type": "tar",
                             "branch": "master"}

        test_compose_success = {"blueprint_name": "example-custom-base",
                                "compose_type": "tar",
                                "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=1",
                                data=json.dumps(test_compose_fail),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to start test compose: %s" % data)

        build_id_fail = data["build_id"]

        resp = self.server.get("/api/v0/compose/queue")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["new"] + data["run"]]
        self.assertEqual(build_id_fail in ids, True, "Failed to add build to the queue")

        # Wait for it to start
        self.assertEqual(_wait_for_status(self, build_id_fail, ["RUNNING"]), True, "Failed to start test compose")

        # Wait for it to finish
        self.assertEqual(_wait_for_status(self, build_id_fail, ["FAILED"]), True, "Failed to finish test compose")

        # Fire up the other one
        resp = self.server.post("/api/v0/compose?test=2",
                                data=json.dumps(test_compose_success),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to start test compose: %s" % data)

        build_id_success = data["build_id"]

        resp = self.server.get("/api/v0/compose/queue")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["new"] + data["run"]]
        self.assertEqual(build_id_success in ids, True, "Failed to add build to the queue")

        # Wait for it to start
        self.assertEqual(_wait_for_status(self, build_id_success, ["RUNNING"]), True, "Failed to start test compose")

        # Wait for it to finish
        self.assertEqual(_wait_for_status(self, build_id_success, ["FINISHED"]), True, "Failed to finish test compose")

        # Test that both composes appear in /api/v0/compose/status/*
        resp = self.server.get("/api/v0/compose/status/*")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["uuids"]]
        self.assertIn(build_id_success, ids, "Finished build not listed by /compose/status/*")
        self.assertIn(build_id_fail, ids, "Failed build not listed by /compose/status/*")

        # Filter by name
        resp = self.server.get("/api/v0/compose/status/*?blueprint=%s" % test_compose_fail["blueprint_name"])
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["uuids"]]
        self.assertIn(build_id_fail, ids, "Failed build not listed by /compose/status blueprint filter")
        self.assertNotIn(build_id_success, ids, "Finished build listed by /compose/status blueprint filter")

        # Filter by type
        resp = self.server.get("/api/v0/compose/status/*?type=tar")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["uuids"]]
        self.assertIn(build_id_fail, ids, "Failed build not listed by /compose/status type filter")
        self.assertIn(build_id_success, ids, "Finished build not listed by /compose/status type filter")

        resp = self.server.get("/api/v0/compose/status/*?type=snakes")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["uuids"]]
        self.assertEqual(ids, [], "Invalid type not filtered by /compose/status type filter")

        # Filter by status
        resp = self.server.get("/api/v0/compose/status/*?status=FAILED")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["uuids"]]
        self.assertIn(build_id_fail, ids, "Failed build not listed by /compose/status status filter")
        self.assertNotIn(build_id_success, "Finished build listed by /compose/status status filter")

    def test_compose_14_kernel_append(self):
        """Test the /api/v0/compose with kernel append customization"""
        test_compose = {"blueprint_name": "example-append",
                        "compose_type": "tar",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=2",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to start test compose: %s" % data)

        build_id = data["build_id"]

        # Is it in the queue list (either new or run is fine, based on timing)
        resp = self.server.get("/api/v0/compose/queue")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["new"] + data["run"]]
        self.assertEqual(build_id in ids, True, "Failed to add build to the queue")

        # Wait for it to start
        self.assertEqual(_wait_for_status(self, build_id, ["RUNNING"]), True, "Failed to start test compose")

        # Wait for it to finish
        self.assertEqual(_wait_for_status(self, build_id, ["FINISHED"]), True, "Failed to finish test compose")

        resp = self.server.get("/api/v0/compose/info/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["queue_status"], "FINISHED", "Build not in FINISHED state")

        # Examine the final-kickstart.ks for the customizations
        # A bit kludgy since it examines the filesystem directly, but that's better than unpacking the metadata
        final_ks = open(joinpaths(self.repo_dir, "var/lib/lorax/composer/results/", build_id, "final-kickstart.ks")).read()

        # Check for the expected customizations in the kickstart
        # nosmt=force should be in the bootloader line, find it and check it
        bootloader_line = ""
        for line in final_ks.splitlines():
            if line.startswith("bootloader"):
                bootloader_line = line
                break
        self.assertNotEqual(bootloader_line, "", "No bootloader line found")
        self.assertTrue("nosmt=force" in bootloader_line)

        # Examine the config.toml to make sure it was written there as well
        cfg_path = joinpaths(self.repo_dir, "var/lib/lorax/composer/results/", build_id, "config.toml")
        cfg_dict = toml.loads(open(cfg_path, "r").read())
        self.assertTrue("extra_boot_args" in cfg_dict)
        self.assertEqual(cfg_dict["extra_boot_args"], "nosmt=force")

    def assertInputError(self, resp):
        """Check all the conditions for a successful input check error result"""
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(data["status"], False)
        self.assertTrue(len(data["errors"]) > 0)
        self.assertTrue("Invalid characters in" in data["errors"][0]["msg"])

    def test_blueprints_list_branch(self):
        resp = self.server.get("/api/v0/blueprints/list?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_info_input(self):
        """Test the blueprints/info input character checking"""
        # /api/v0/blueprints/info/<blueprint_names>
        resp = self.server.get("/api/v0/blueprints/info/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.get("/api/v0/blueprints/info/example-http-server?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.get("/api/v0/blueprints/info/example-http-server?format=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_changes_input(self):
        """Test the blueprints/changes input character checking"""
        # /api/v0/blueprints/changes/<blueprint_names>
        resp = self.server.get("/api/v0/blueprints/changes/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.get("/api/v0/blueprints/changes/example-http-server?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_new_input(self):
        """Test the blueprints/new input character checking"""
        # /api/v0/blueprints/new
        test_blueprint = {"description": "An example GlusterFS server with samba",
                       "name":UTF8_TEST_STRING,
                       "version": "0.2.0",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB],
                       "groups": []}

        resp = self.server.post("/api/v0/blueprints/new",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        self.assertInputError(resp)

        test_blueprint["name"] = "example-glusterfs"
        resp = self.server.post("/api/v0/blueprints/new?branch=" + UTF8_TEST_STRING,
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        self.assertInputError(resp)

    def test_blueprints_delete_input(self):
        """Test the blueprints/delete input character checking"""
        resp = self.server.delete("/api/v0/blueprints/delete/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.delete("/api/v0/blueprints/delete/example-http-server?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_workspace_input(self):
        """Test the blueprints/workspace input character checking"""
        test_blueprint = {"description": "An example GlusterFS server with samba, ws version",
                       "name":UTF8_TEST_STRING,
                       "version": "0.3.0",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB],
                       "groups": []}

        resp = self.server.post("/api/v0/blueprints/workspace",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        self.assertInputError(resp)

        test_blueprint["name"] = "example-glusterfs"
        resp = self.server.post("/api/v0/blueprints/workspace?branch=" + UTF8_TEST_STRING,
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        self.assertInputError(resp)

    def test_blueprints_workspace_delete_input(self):
        """Test the DELETE blueprints/workspace input character checking"""
        resp = self.server.delete("/api/v0/blueprints/workspace/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.delete("/api/v0/blueprints/workspace/example-http-server?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_undo_input(self):
        """Test the blueprints/undo/... input character checking"""
        resp = self.server.post("/api/v0/blueprints/undo/" + UTF8_TEST_STRING + "/deadbeef")
        self.assertInputError(resp)

        resp = self.server.post("/api/v0/blueprints/undo/example-http-server/deadbeef?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_tag_input(self):
        """Test the blueprints/tag input character checking"""
        resp = self.server.post("/api/v0/blueprints/tag/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.post("/api/v0/blueprints/tag/example-http-server?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_diff_input(self):
        """Test the blueprints/diff input character checking"""
        # /api/v0/blueprints/diff/<blueprint_name>/<from_commit>/<to_commit>
        resp = self.server.get("/api/v0/blueprints/diff/" + UTF8_TEST_STRING + "/NEWEST/WORKSPACE")
        self.assertInputError(resp)

        resp = self.server.get("/api/v0/blueprints/diff/example-http-server/NEWEST/WORKSPACE?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_freeze_input(self):
        """Test the blueprints/freeze input character checking"""
        resp = self.server.get("/api/v0/blueprints/freeze/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.get("/api/v0/blueprints/freeze/example-http-server?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.get("/api/v0/blueprints/freeze/example-http-server?format=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_blueprints_depsolve_input(self):
        """Test the blueprints/depsolve input character checking"""
        resp = self.server.get("/api/v0/blueprints/depsolve/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

        resp = self.server.get("/api/v0/blueprints/depsolve/example-http-server?branch=" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_projects_info_input(self):
        """Test the projects/info input character checking"""
        resp = self.server.get("/api/v0/projects/info/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_projects_depsolve_input(self):
        """Test the projects/depsolve input character checking"""
        resp = self.server.get("/api/v0/projects/depsolve/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_projects_source_info_input(self):
        """Test the projects/source/info input character checking"""
        resp = self.server.get("/api/v0/projects/source/info/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_projects_source_delete_input(self):
        """Test the projects/source/delete input character checking"""
        resp = self.server.delete("/api/v0/projects/source/delete/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_modules_list_input(self):
        """Test the modules/list input character checking"""
        resp = self.server.get("/api/v0/modules/list/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_modules_info_input(self):
        """Test the modules/info input character checking"""
        resp = self.server.get("/api/v0/modules/info/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_status_input(self):
        """Test the compose/status input character checking"""
        resp = self.server.get("/api/v0/compose/status/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_cancel_input(self):
        """Test the compose/cancel input character checking"""
        resp = self.server.delete("/api/v0/compose/cancel/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_delete_input(self):
        """Test the compose/delete input character checking"""
        resp = self.server.delete("/api/v0/compose/delete/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_info_input(self):
        """Test the compose/info input character checking"""
        resp = self.server.get("/api/v0/compose/info/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_metadata_input(self):
        """Test the compose/metadata input character checking"""
        resp = self.server.get("/api/v0/compose/metadata/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_results_input(self):
        """Test the compose/results input character checking"""
        resp = self.server.get("/api/v0/compose/results/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_logs_input(self):
        """Test the compose/logs input character checking"""
        resp = self.server.get("/api/v0/compose/logs/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_image_input(self):
        """Test the compose/image input character checking"""
        resp = self.server.get("/api/v0/compose/image/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    def test_compose_log_input(self):
        """Test the compose/log input character checking"""
        resp = self.server.get("/api/v0/compose/log/" + UTF8_TEST_STRING)
        self.assertInputError(resp)

    # A series of tests for dealing with deleted blueprints
    def test_deleted_bp_00_setup(self):
        """Setup a deleted blueprint for use in the tests"""
        # Start by creating a new blueprint for this series of tests and then
        # deleting it.
        test_blueprint = {"description": "A blueprint that has been deleted",
                       "name":"deleted-blueprint",
                       "version": "0.0.1",
                       "modules":[GLUSTERFS_GLOB,
                                  GLUSTERFSFUSE_GLOB],
                       "packages":[SAMBA_GLOB,
                                   TMUX_GLOB],
                       "groups": []}

        resp = self.server.post("/api/v0/blueprints/new",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.delete("/api/v0/blueprints/delete/deleted-blueprint")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

    def test_deleted_bp_01_show(self):
        """Test blueprint show with deleted blueprint"""
        resp = self.server.get("/api/v0/blueprints/info/deleted-blueprint")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue(len(data["errors"]) > 0)
        self.assertEqual(data["errors"][0]["id"], "UnknownBlueprint")

    def test_deleted_bp_02_depsolve(self):
        """Test blueprint depsolve with deleted blueprint"""
        resp = self.server.get("/api/v0/blueprints/depsolve/deleted-blueprint")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue(len(data["errors"]) > 0)
        self.assertEqual(data["errors"][0]["id"], "UnknownBlueprint")

    def test_deleted_bp_03_diff(self):
        """Test blueprint diff with deleted blueprint"""
        resp = self.server.get("/api/v0/blueprints/diff/deleted-blueprint/NEWEST/WORKSPACE")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue(len(data["errors"]) > 0)
        self.assertEqual(data["status"], False)
        self.assertEqual(data["errors"][0]["id"], "UnknownBlueprint")

    def test_deleted_bp_04_freeze(self):
        """Test blueprint freeze with deleted blueprint"""
        resp = self.server.get("/api/v0/blueprints/freeze/deleted-blueprint")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue(len(data["errors"]) > 0)
        self.assertEqual(data["errors"][0]["id"], "UnknownBlueprint")

    def test_deleted_bp_05_tag(self):
        """Test blueprint tag with deleted blueprint"""
        resp = self.server.post("/api/v0/blueprints/tag/deleted-blueprint")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertTrue(len(data["errors"]) > 0)
        self.assertEqual(data["errors"][0]["id"], "UnknownBlueprint")

@contextmanager
def in_tempdir(prefix='tmp'):
    """Execute a block of code with chdir in a temporary location"""
    oldcwd = os.getcwd()
    tmpdir = tempfile.mkdtemp(prefix=prefix)
    os.chdir(tmpdir)
    yield
    os.chdir(oldcwd)
    shutil.rmtree(tmpdir)

def makeFakeRPM(repo_dir, name, epoch, version, release):
    """Make a fake rpm file in repo_dir"""
    p = SimpleRpmBuild(name, version, release)
    if epoch:
        p.epoch = epoch
    p.add_simple_payload_file_random()
    with in_tempdir("lorax-test-rpms."):
        p.make()
        rpmfile = p.get_built_rpm(expectedArch)
        shutil.move(rpmfile, repo_dir)

class RepoCacheTestCase(unittest.TestCase):
    """Test to make sure that changes to the repository are picked up immediately."""
    @classmethod
    def setUpClass(self):
        repo_dir = tempfile.mkdtemp(prefix="lorax.test.repo.")
        server.config["REPO_DIR"] = repo_dir
        repo = open_or_create_repo(server.config["REPO_DIR"])
        server.config["GITLOCK"] = GitLock(repo=repo, lock=Lock(), dir=repo_dir)

        server.config["COMPOSER_CFG"] = configure(root_dir=repo_dir, test_config=True)
        os.makedirs(joinpaths(server.config["COMPOSER_CFG"].get("composer", "share_dir"), "composer"))
        errors = make_queue_dirs(server.config["COMPOSER_CFG"], 0)
        if errors:
            raise RuntimeError("\n".join(errors))

        make_dnf_dirs(server.config["COMPOSER_CFG"])

        # Modify fedora vs. rawhide tests when running on rawhide
        if os.path.exists("/etc/yum.repos.d/fedora-rawhide.repo"):
            self.rawhide = True

        # Create an extra repo to use for checking the metadata expire handling
        os.makedirs("/tmp/lorax-test-repo/")
        makeFakeRPM("/tmp/lorax-test-repo/", "fake-milhouse", 0, "1.0.0", "1")
        os.system("createrepo_c /tmp/lorax-test-repo/")

        server.config["DNFLOCK"] = DNFLock(server.config["COMPOSER_CFG"], expire_secs=10)

        # Include a message in /api/status output
        server.config["TEMPLATE_ERRORS"] = ["Test message"]

        server.config['TESTING'] = True
        self.server = server.test_client()
        self.repo_dir = repo_dir

        # Copy the shared files over to the directory tree we are using
        share_path = "./share/composer/"
        for f in glob(joinpaths(share_path, "*")):
            shutil.copy(f, joinpaths(server.config["COMPOSER_CFG"].get("composer", "share_dir"), "composer"))

        start_queue_monitor(server.config["COMPOSER_CFG"], 0, 0)

    @classmethod
    def tearDownClass(self):
        shutil.rmtree(server.config["REPO_DIR"])
        shutil.rmtree("/tmp/lorax-test-repo/")

    def add_new_source(self, repo_dir):
        json_source = """{"name": "new-repo-1", "url": "file:///tmp/lorax-test-repo/", "type": "yum-baseurl",
                          "check_ssl": false, "check_gpg": false}"""
        self.assertTrue(len(json_source) > 0)
        resp = self.server.post("/api/v0/projects/source/new",
                                data=json_source,
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

    def add_blueprint(self):
        test_blueprint = {"description": "Metadata expire test blueprint",
                          "name":"milhouse-test",
                          "version": "0.0.1",
                          "modules":[],
                          "packages":[{"name":"fake-milhouse", "version":"1.*.*"}],
                          "groups": []}

        resp = self.server.post("/api/v0/blueprints/new",
                                data=json.dumps(test_blueprint),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

    def test_metadata_expires(self):
        """Ensure that metadata expire settings pick up changes to the repo immediately"""

        # Metadata can change at any time, but checking for that is expensive. So we only want
        # to check when the timeout has expired, OR when starting a new compose
        # Add a new repository at /tmp/lorax-test-repo/
        self.add_new_source("/tmp/lorax-test-repo")

        # Add a new blueprint with fake-milhouse in it
        self.add_blueprint()

        # Depsolve the blueprint
        resp = self.server.get("/api/v0/blueprints/depsolve/milhouse-test")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0]["blueprint"]["name"], "milhouse-test")
        deps = blueprints[0]["dependencies"]
        print(deps)
        self.assertTrue(any([True for d in deps if d["name"] == "fake-milhouse" and d["version"] == "1.0.0"]))
        self.assertFalse(data.get("errors"))

        # Make a new version of fake-milhouse
        makeFakeRPM("/tmp/lorax-test-repo/", "fake-milhouse", 0, "1.0.1", "1")
        os.system("createrepo_c /tmp/lorax-test-repo/")

        # Expire time has been set to 10 seconds, so wait 11 and try it.
        time.sleep(11)

        resp = self.server.get("/api/v0/blueprints/depsolve/milhouse-test")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0]["blueprint"]["name"], "milhouse-test")
        deps = blueprints[0]["dependencies"]
        print(deps)
        self.assertTrue(any([True for d in deps if d["name"] == "fake-milhouse" and d["version"] == "1.0.1"]))
        self.assertFalse(data.get("errors"))

        # Make a new version of fake-milhouse
        makeFakeRPM("/tmp/lorax-test-repo/", "fake-milhouse", 0, "1.0.2", "1")
        os.system("createrepo_c /tmp/lorax-test-repo/")

        test_compose = {"blueprint_name": "milhouse-test",
                        "compose_type": "tar",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=2",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to start test compose: %s" % data)

        build_id = data["build_id"]

        # Check to see which version was used for the compose, should be 1.0.2
        resp = self.server.get("/api/v0/compose/info/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        pkg_deps = data["deps"]["packages"]
        print(pkg_deps)
        self.assertTrue(any([True for d in pkg_deps if d["name"] == "fake-milhouse" and d["version"] == "1.0.2"]))

class GitRPMBlueprintTestCase(unittest.TestCase):
    """Test to make sure that a blueprint with repos.git entry works."""
    @classmethod
    def setUpClass(self):
        (self.gitrpm_repo, self.test_results, self.first_commit) = create_git_repo()

        repo_dir = tempfile.mkdtemp(prefix="lorax.test.repo.")
        server.config["REPO_DIR"] = repo_dir
        repo = open_or_create_repo(server.config["REPO_DIR"])
        server.config["GITLOCK"] = GitLock(repo=repo, lock=Lock(), dir=repo_dir)

        server.config["COMPOSER_CFG"] = configure(root_dir=repo_dir, test_config=True)
        os.makedirs(joinpaths(server.config["COMPOSER_CFG"].get("composer", "share_dir"), "composer"))
        errors = make_queue_dirs(server.config["COMPOSER_CFG"], os.getgid())
        if errors:
            raise RuntimeError("\n".join(errors))

        make_dnf_dirs(server.config["COMPOSER_CFG"])

        # Modify fedora vs. rawhide tests when running on rawhide
        if os.path.exists("/etc/yum.repos.d/fedora-rawhide.repo"):
            self.rawhide = True

        server.config["DNFLOCK"] = DNFLock(server.config["COMPOSER_CFG"], expire_secs=10)

        # Include a message in /api/status output
        server.config["TEMPLATE_ERRORS"] = ["Test message"]

        server.config['TESTING'] = True
        self.server = server.test_client()
        self.repo_dir = repo_dir

        # Copy the shared files over to the directory tree we are using
        share_path = "./share/composer/"
        for f in glob(joinpaths(share_path, "*")):
            shutil.copy(f, joinpaths(server.config["COMPOSER_CFG"].get("composer", "share_dir"), "composer"))

        start_queue_monitor(server.config["COMPOSER_CFG"], 0, 0)

    @classmethod
    def tearDownClass(self):
        shutil.rmtree(server.config["REPO_DIR"])
        shutil.rmtree(self.gitrpm_repo)

    def test_01_depsolve_gitrpm(self):
        """Make sure that depsolve works with repos.git"""
        # Note that the git rpm isn't built and added until a compose, so it won't be listed
        test_blueprint = """
            name = "git-rpm-blueprint-test"
            description = "A test blueprint including a rpm created from git"
            version = "0.0.1"

            [[repos.git]]
            rpmname="git-rpm-test"
            rpmversion="1.0.0"
            rpmrelease="1"
            summary="Testing the git rpm code"
            repo="file://%s"
            ref="%s"
            destination="/srv/testing-rpm/"

            [[packages]]
            name="openssh-server"
            version="*"
        """ % (self.gitrpm_repo, self.first_commit)
        resp = self.server.post("/api/v0/blueprints/new",
                                data=test_blueprint,
                                content_type="text/x-toml")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        resp = self.server.get("/api/v0/blueprints/depsolve/git-rpm-blueprint-test")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        blueprints = data.get("blueprints")
        self.assertNotEqual(blueprints, None)
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0]["blueprint"]["name"], "git-rpm-blueprint-test")
        self.assertFalse(data.get("errors"))
        deps = blueprints[0]["dependencies"]
        print(deps)
        self.assertEqual(len(blueprints[0]["dependencies"]) > 10, True)

    def test_02_compose_gitrpm(self):
        """Test that the compose includes the git rpm repo and rpm"""
        test_compose = {"blueprint_name": "git-rpm-blueprint-test",
                        "compose_type": "tar",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=2",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], True, "Failed to start test compose: %s" % data)

        build_id = data["build_id"]

        # Is it in the queue list (either new or run is fine, based on timing)
        resp = self.server.get("/api/v0/compose/queue")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        ids = [e["id"] for e in data["new"] + data["run"]]
        self.assertEqual(build_id in ids, True, "Failed to add build to the queue")

        # Wait for it to start
        self.assertEqual(_wait_for_status(self, build_id, ["RUNNING"]), True, "Failed to start test compose")

        # Wait for it to finish
        self.assertEqual(_wait_for_status(self, build_id, ["FINISHED"]), True, "Failed to finish test compose")

        resp = self.server.get("/api/v0/compose/info/%s" % build_id)
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["queue_status"], "FINISHED", "Build not in FINISHED state")

        # Examine the final-kickstart.ks for the customizations
        # A bit kludgy since it examines the filesystem directly, but that's better than unpacking the metadata
        final_ks = open(joinpaths(self.repo_dir, "var/lib/lorax/composer/results/", build_id, "final-kickstart.ks")).read()

        # Is the source in the kickstart?
        self.assertTrue('repo --name="gitrpms"' in final_ks)

        # Is the rpm in the kickstart?
        self.assertTrue("git-rpm-test-1.0.0-1" in final_ks)

    def test_03_compose_badref_gitrpm(self):
        """Make sure that compose with a bad reference returns an error"""
        test_blueprint = """
            name = "git-rpm-blueprint-test"
            description = "A test blueprint including a rpm created from git"
            version = "0.0.2"

            [[repos.git]]
            rpmname="git-rpm-test"
            rpmversion="1.0.0"
            rpmrelease="1"
            summary="Testing the git rpm code"
            repo="file://%s"
            ref="nobody-saw-me-do-it"
            destination="/srv/testing-rpm/"

            [[packages]]
            name="openssh-server"
            version="*"
        """ % self.gitrpm_repo
        resp = self.server.post("/api/v0/blueprints/new",
                                data=test_blueprint,
                                content_type="text/x-toml")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        test_compose = {"blueprint_name": "git-rpm-blueprint-test",
                        "compose_type": "tar",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=2",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False)
        self.assertTrue("errors" in data and len(data["errors"]) > 0)
        self.assertEqual(data["errors"][0]["id"], "BuildFailed")

    def test_04_compose_badrepo_gitrpm(self):
        """Make sure that compose with a bad repo returns an error"""
        test_blueprint = """
            name = "git-rpm-blueprint-test"
            description = "A test blueprint including a rpm created from git"
            version = "0.0.3"

            [[repos.git]]
            rpmname="git-rpm-test"
            rpmversion="1.0.0"
            rpmrelease="1"
            summary="Testing the git rpm code"
            repo="file:///not/a/repo/path/"
            ref="origin/master"
            destination="/srv/testing-rpm/"

            [[packages]]
            name="openssh-server"
            version="*"
        """
        resp = self.server.post("/api/v0/blueprints/new",
                                data=test_blueprint,
                                content_type="text/x-toml")
        data = json.loads(resp.data)
        self.assertEqual(data, {"status":True})

        test_compose = {"blueprint_name": "git-rpm-blueprint-test",
                        "compose_type": "tar",
                        "branch": "master"}

        resp = self.server.post("/api/v0/compose?test=2",
                                data=json.dumps(test_compose),
                                content_type="application/json")
        data = json.loads(resp.data)
        self.assertNotEqual(data, None)
        self.assertEqual(data["status"], False)
        self.assertTrue("errors" in data and len(data["errors"]) > 0)
        self.assertEqual(data["errors"][0]["id"], "BuildFailed")
