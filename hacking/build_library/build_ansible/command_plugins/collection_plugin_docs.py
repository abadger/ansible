# coding: utf-8
# Copyright: (c) 2019, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import asyncio
import itertools
import os
import os.path
import pathlib
import sys
import tarfile
from collections import defaultdict
from distutils.version import LooseVersion
from functools import partial
from tempfile import TemporaryDirectory

import yaml
from jinja2 import Environment, FileSystemLoader
from ansible.galaxy.api import GalaxyAPI
from ansible.module_utils._text import to_bytes, to_native
from ansible.module_utils.urls import Request

# Pylint doesn't understand Python3 namespace modules.
# pylint: disable=relative-beyond-top-level
from ..change_detection import update_file_if_different
from ..commands import Command
from ..jinja2.environment import doc_environment
from ..jinja2.filters import documented_type, html_ify
from ..document_plugins import (get_plugin_info, init_plugin_doc_arg_parser,
                                normalize_plugin_info, plugin_filename_format,
                                plugin_output_directory)
# pylint: enable=relative-beyond-top-level


# This is purely for testing whether it makes more sense to place collection docs in a subdirectory
# or all in one directory.  Once in production, we will choose one and get rid of this switch
_STYLE_DIRECTORY = True if os.environ.get('_STYLE_DIRECTORY', False) else False

PLUGIN_TYPES = frozenset(('become', 'cache', 'callback', 'cliconf', 'connection', 'httpapi',
                          'inventory', 'lookup', 'shell', 'strategy', 'vars', 'module',
                          'module_utils'))
DOCUMENTED_PLUGIN_TYPES = frozenset(('module',))
DEFAULT_PLUGIN_TO_COLLECTION_FILE = (pathlib.Path(__file__).parents[4]
                                     / 'docs/docsite/collection-plugins.yml')

DOCS_SITE = 'https://docs.ansible.com/ansible/collections/plugins/'
GALAXY_SERVER = 'galaxy.ansible.com'
MAX_PAGE_SIZE = 1000  # The galaxy server has a max page size

# Testing config
GALAXY_SERVER = 'galaxy-dev.ansible.com'

DEFAULT_TEMPLATE_DIR = pathlib.Path(__file__).parents[4] / 'docs/templates'
DEFAULT_STUB_TEMPLATE_FILE = 'moved_to_collections.rst.j2'
DEFAULT_PLUGIN_TEMPLATE_FILE = 'collectionized_plugin.rst.j2'
DEFAULT_OUTPUT_DIR = pathlib.Path(__file__).parents[4] / 'docs/docsite'


#
# Exceptions
#

class UnreleasedCollectionError(Exception):
    """Raised when a collection has no releases available"""
    pass


class UnknownCollectionNameFormat(Exception):
    """A collection's tarball name was in an unanticipated format"""
    pass


#
# Common utility functions
#

def plugin_to_collection_mapping(mapping_filename):
    """
    Return the data that maps all plugins inside of ansible to their collection location.

    :arg mapping_filename: The filename in which the data lives
    :return: The returned data is structured as a set of dicts::

        $plugin_type:
            $plugin_name:
                collection:
                    source: "Service that this lives on.  For example https://galaxy.ansible.com/"
                    fqcn: "Full qualified collection name.  For example purestorage.flasharray"
            $plugin_name2:
            [..]
        $plugin_type2:
        [..]

    """
    # Read the data in
    with open(mapping_filename, 'rb') as f:
        plugin_mapping = yaml.safe_load(f.read())

    # Normalize the data to a form we'll use later
    for plugin_record in plugin_mapping.values():
        for plugin_name, collection_info in plugin_record.items():
            plugin_record[plugin_name] = {'collection': collection_info, 'plugin': {}}

    return plugin_mapping


def get_running_loop():
    """
    Do our best to get the running loop

    Python-3.7 has a special function for this but older Pythons have a different function which is
    not robust in some cornercases which we hopefully will not encounter.
    """
    if sys.version_info >= (3, 7):
        return asyncio.get_running_loop()
    return asyncio.get_event_loop()


class CollectionFetcher:
    """
    Fetches collections from a galaxy server to a directory.

    .. warn:: This class is used in multithreaded code.  It is designed to be initialized and then
        none of the attributes it contains are changed by any of the methods.  This makes the
        methods safe for use inside of multithreaded code.  If you need to use different attributes,
        you need to use a new instance of this object.  If you are writing new methods, be sure your
        methods do not write to any of the attributes.
    """
    CHUNK_SIZE = 4096

    def __init__(self, server, destdir):
        """
        :server: The galaxy server to fetch from
        :destdir: The destination directory to fetch to
        """
        self.api = GalaxyAPI(None, None, server)
        self.destdir = destdir

    def _construct_filename(self, download_url):
        """Construct a filename for the collection inside of the destdir"""
        base_filename = os.path.basename(download_url)
        return os.path.join(self.destdir, base_filename)

    def fetch_collection(self, namespace, name, newer_than=None):
        """
        Retrieves the latest version of the collection to the CollectionFetcher's destdir

        :arg namespace: Namespace of the collection
        :arg name: Name of the collection
        :arg newer_then: If this is set, it must be a version string understood by
            :py3:`distutils.versioning.LooseVersion` we only download a collection if it is newer
            than this version string.  If this is not set, then we always download the latest
            version of the collection.
        :returns: filename of the fetched collection or None if no collection was retrieved
        :raises UnreleasedCollectionError: if the specified collection has no releases to download
        """
        # Find the latest version of a collection.
        # Requires one round trip
        versions = self.api.get_collection_versions(namespace, name)
        if len(versions) <= 0:
            raise UnreleasedCollectionError('Collection {namespace}.{name} does not yet have any'
                                            ' releases'.format(namespace=namespace, name=name))
        latest = sorted(versions, key=LooseVersion)[0]

        if newer_than and LooseVersion(newer_than) > LooseVersion(latest):
            return None

        # Get the download url for the latest version of the collection
        # Roundtrip #2
        collection_info = self.api.get_collection_version_metadata(namespace, name, latest)
        filename = self._construct_filename(collection_info.download_url)

        requestor = Request()
        response_handle = requestor.get(collection_info.download_url)
        with open(filename, 'wb') as f:
            # Replace with walrus operator when we can require python-3.8
            # while chunk := response_handle.read(self.CHUNK_SIZE):
            #   f.write(chunk)
            chunk = response_handle.read(self.CHUNK_SIZE)
            while chunk:
                f.write(chunk)
                chunk = response_handle.read(self.CHUNK_SIZE)

        return filename


class CollectionWriter:
    def __init__(self, template_dir, output_dir):
        """
        Context and initialization for writing collection docs
        :arg jinja_env: The jinja2 environment in which te template for the stub pages resides
        """
        self.jinja_env = doc_environment(template_dir)
        self.output_dir = output_dir
        self.main_docsite_dir = os.path.join(output_dir, 'rst')
        self.collection_doc_dir = os.path.join(output_dir, 'collections')

        # Create collection_doc_dir if it does not exist
        try:
            os.mkdir(self.collection_doc_dir, mode=0o755)
        except FileExistsError:
            # Sanity check that the file is not a symlink
            if os.path.islink(self.collection_doc_dir):
                raise ValueError('DANGER: The collection path in output_dir already exists as'
                                 ' a symlink!')

    def write_stubs(self, plugin_type, plugin_name, collection_name):
        """
        Output an html page at the old location that says the plugin has moved to a collection

        :arg plugin_type: Is the plugin a module, module_util, callback, connection, etc
        :arg plugin_name: The name of the plugin
        :arg collection_name: FQCN for the collection which contains the plugin
        """
        # The directory to output the stub file to.
        stub_dir = plugin_output_directory(plugin_type, self.main_docsite_dir)

        if not os.path.exists(stub_dir):
            os.mkdir(stub_dir)

        stub_template = self.jinja_env.get_template('moved_to_collections.rst.j2')
        stub_file = stub_template.render({'plugin_type': plugin_type,
                                          'module': plugin_name,
                                          'collection': collection_name,
                                          })

        output_file = plugin_filename_format(plugin_type) % plugin_name
        filename = os.path.join(stub_dir, output_file)
        with open(filename, 'w') as f:
            f.write(stub_file)

    def write_collection_plugin_doc(self, plugin_type, plugin_name, plugin_record):
        """
        Write the documentation for a plugin that is in a collection

        :arg plugin_type: Is the plugin a module, module_util, callback, connection, etc
        :arg plugin_name: The name of the plugin
        :arg plugin_record: Information about the plugin
        """
        # Set the directory that the plugin doc will be written to
        plugin_doc_dir = os.path.join(self.collection_doc_dir, plugin_record['collection']['fqcn'])
        if not os.path.exists(plugin_doc_dir):
            os.mkdir(plugin_doc_dir)

        if _STYLE_DIRECTORY:
            plugin_doc_dir = os.path.join(plugin_doc_dir, plugin_type)
            if not os.path.exists(plugin_doc_dir):
                os.mkdir(plugin_doc_dir)

        # Pass the documentation into the jinja2 render function
        plugin_template = self.jinja_env.get_template('plugin.rst.j2')
        plugin_file = plugin_template.render(**plugin_record['plugin'])

        # Write the docs to the plugin_name.rst file
        if _STYLE_DIRECTORY:
            filename = os.path.join(plugin_doc_dir, '%s.rst' % plugin_name)
        else:
            filename = os.path.join(plugin_doc_dir,
                                    '%s_%s.rst' % (plugin_name, plugin_type))
        with open(filename, 'w') as f:
            f.write(plugin_file)

    def write_index(self, collection_name, collection_plugins):
        """
        Write an index file listing docs in the collection

        :arg collection_name: FQCN of the collection
        :arg collection_plugins: Information about the plugins that are in the collection
        """
        # Pass the documentation into the jinja2 render function
        collection_template = self.jinja_env.get_template('plugins_by_collection.rst.j2')
        collection_info = {'collection_name': collection_name, 'plugins': collection_plugins}
        collection_file = collection_template.render(**collection_info)

        # Write the docs to the plugin_name.rst file
        if _STYLE_DIRECTORY:
            # FIXME: Have to do more here:
            # toplevel index.rst
            # sub index.rst for each plugin type
            filename = os.path.join(self.collection_doc_dir, collection_name, 'index.rst')
        else:
            filename = os.path.join(self.collection_doc_dir, collection_name, 'index.rst')
        with open(filename, 'w') as f:
            f.write(collection_file)

    def write_toplevel_index(self, collections):
        """
        Write an index file listing all of the collections we have docs for

        :arg collections: List of fqcns.
        """
        # Pass the documentation into the jinja2 render function
        collection_template = self.jinja_env.get_template('list_of_collections.rst.j2')
        collection_file = collection_template.render(collections=collections)

        filename = os.path.join(self.collection_doc_dir, 'index.rst')
        with open(filename, 'w') as f:
            f.write(collection_file)


#
# Subcommand validate
#

def validate(args):
    """
    Validate the structure of the yaml file

    :args args: The parsed command line arguments

    Schema example:
    .. code-block:: yaml

        modules:
          ec2:
            fqcn: "ansible_aws_sig.amazon"
            source: "https://galaxy.ansible.com"
          ec2_remove_host:
            fqcn: "ansible_aws_sig.ec2"
            source: "https://dev.ansible.redhat.com/api/automation-hub"
        filters:
          ipaddress:
            fqcn: "ansible_ipadress.ipaddress"
            source: "https://galaxy.ansible.com"
    """
    # Import voluptuous here so that it is only a dependency of the validation, not of the rest of
    # the script
    import voluptuous as v

    collection_mapping_schema = v.Schema({v.Any(*PLUGIN_TYPES): {
        str: {'fqcn': str,
              'source': str}
    }})

    with open(args.plugin_to_collection_file, 'rb') as f:
        plugin_mapping = yaml.safe_load(f.read())
    return collection_mapping_schema(plugin_mapping)


#
# Subcommand full
#

async def fetch_collections(destdir, fqcns):
    """
    Fetch all of the collections to the destdir
    """
    loop = get_running_loop()

    requestors = []
    for server in fqcns:
        fetcher = CollectionFetcher(server, destdir)
        for fqcn in fqcns[server]:
            requestors.append(loop.run_in_executor(None, fetcher.fetch_collection,
                                                   *fqcn.split('.', 1)))

    try:
        await asyncio.gather(*requestors, return_exceptions=False)
    except Exception as e:
        print("*** Error: Unable to download collection: %s" % to_native(e))
        tasks = []
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
                tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()


def expand_collection(tar_file, expand_dir):
    # TODO: Is the filename normalized by galaxy or could we get a filename that was arbitrarily
    # determined by the uploading user?
    # According to rich, the filename is validated by galaxy so if it doesn't match
    # namespace.collection-version.tar.gz, then it is rejected.

    # Derive the fqcn from the tar_file name
    tar_name_parts = os.path.basename(tar_file).split('-')
    if len(tar_name_parts) != 3:
        raise UnknownCollectionNameFormat('The name of the tarball for a collection did not meet'
                                          ' our assumptions.  This code has to be recoded to allow'
                                          ' for this name: {0}'.format(tar_file))
    fqcn = '.'.join(tar_name_parts[0:2])

    dir_name = os.path.join(expand_dir, fqcn)

    with tarfile.open(tar_file, 'r') as collection_tar:
        return collection_tar.extractall(path=dir_name)


async def expand_collections(tar_dir, expand_dir):
    """
    Expand all collections in the tar_dir into expand_dir

    :tar_dir: Directory where the collections have been placed
    :expand_dir: Directory where the expanded collections will live
    """
    loop = get_running_loop()

    expanders = []
    for filename in os.listdir(tar_dir):
        tar_file = os.path.join(tar_dir, filename)
        expanders.append((tar_file, loop.run_in_executor(None, expand_collection, tar_file,
                                                         expand_dir)))

    results = await asyncio.gather(*[e[-1] for e in expanders], return_exceptions=True)
    for idx, result in enumerate(results):
        if isinstance(result, BaseException):
            collection_tar_file = expanders[idx][0]
            print("*** ERROR: expanding a collection: %s: %s (skipped)***"
                  % (collection_tar_file, to_native(result)))
            continue


def get_plugin_data(plugin_type, plugin_name, collection_name, input_dir):
    # Determine which file in the collection has the plugin's docs
    if plugin_type in ('module', 'module_util'):
        normalized_plugin_type = '{0}s'.format(plugin_type)
    else:
        normalized_plugin_type = plugin_type

    plugin_file = os.path.join(input_dir, collection_name, 'plugins', normalized_plugin_type,
                               '%s.py' % plugin_name)

    # Get the documentation data from the plugin

    # Note: this could raise an exception
    plugin_data = get_plugin_info(plugin_name, plugin_file, input_dir)

    return plugin_data


async def get_all_plugin_data(plugin_mapping, input_dir):
    """
    Retrieve information from all plugins in plugin_mapping from files in input_dir

    :arg plugin_mapping: Mapping of plugin data.
    :arg input_dir: The directory to scan for plugin information

    .. warning::
        This function operates by side effect.  The plugin_mapping is modified to contain the
        new plugin information
    """
    loop = get_running_loop()

    parsers = []
    for plugin_type in DOCUMENTED_PLUGIN_TYPES:
        for plugin_name, plugin_record in plugin_mapping[plugin_type].items():
            collection = plugin_record['collection']
            parsers.append((plugin_name, plugin_type, collection,
                            loop.run_in_executor(None, get_plugin_data, plugin_type, plugin_name,
                                                 collection['fqcn'], input_dir)))

    results = await asyncio.gather(*[p[-1] for p in parsers], return_exceptions=True)
    for idx, result in enumerate(results):
        plugin_name = parsers[idx][0]
        plugin_type = parsers[idx][1]
        collection = parsers[idx][2]

        if isinstance(result, Exception):
            print("*** ERROR: Malformed documentation in %s: %s"
                  " (skipped)***" % (plugin_name, to_native(result)))
            continue

        # Format the data into the form the templates expect
        aliases = set()  # Current collections implementation does not have aliases
        normalize_plugin_info(result['doc'], plugin_name, collection['fqcn'],
                              collection['source'], result['source'], plugin_type,
                              result['deprecated'], aliases, result['metadata'],
                              result['examples'], result['returndocs'])
        plugin_data = result['doc']

        # Add the plugin information to the plugin's record
        plugin_mapping[plugin_type][plugin_name]['plugin'] = plugin_data


async def write_docs(plugin_mapping, template_dir, output_dir):
    loop = get_running_loop()

    writer = CollectionWriter(template_dir, output_dir)

    writers = []
    # We only provide backwards compatible documentation for a subset of the plugin types
    for plugin_type in DOCUMENTED_PLUGIN_TYPES:
        for plugin_name, plugin_record in plugin_mapping[plugin_type].items():
            collection = plugin_record['collection']

            writers.append(loop.run_in_executor(None, writer.write_stubs, plugin_type, plugin_name,
                                                collection['fqcn']))
            # TODO: In the future we may want to look at documenting all of the plugins in the
            # collection, not just the ones that have moved.  This is in part because the index
            # pages are per-collection but they don't currently document plugins in the collection
            # which were not in ansible/ansible to begin with
            writers.append(loop.run_in_executor(None, writer.write_collection_plugin_doc,
                                                plugin_type, plugin_name, plugin_record))

    # Construct a collection mapping which is:
    # collection_name: plugin_type: plugin_name: plugin_info
    collection_mapping = defaultdict(partial(defaultdict, dict))
    # Note: we only document modules right now
    for plugin_type, plugin_info in ((typ, info) for (typ, info) in plugin_mapping.items()
                                     if typ in DOCUMENTED_PLUGIN_TYPES):
        for plugin_name, plugin_record in plugin_info.items():
            collection = plugin_record['collection']
            collection_mapping[collection['fqcn']][plugin_type][plugin_name] = plugin_record['plugin']

    writers.append(loop.run_in_executor(None, writer.write_toplevel_index,
                                        collection_mapping.keys()))

    for collection_name, collection_plugins in collection_mapping.items():
        writers.append(loop.run_in_executor(None, writer.write_index, collection_name,
                                            collection_plugins))

    results = await asyncio.gather(*writers, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            print("*** ERROR: writing documentation: %s (skipped)***" % to_native(result))
            continue


async def generate_full_docs(args):
    """Regenerate the documentation for all plugins listed in the plugin_to_collection_file"""
    fqcns_by_server = defaultdict(set)
    plugin_mapping = plugin_to_collection_mapping(args.plugin_to_collection_file)

    # We only need the collection info from plugin_mapping:
    plugins = (p for p in plugin_mapping.values())  # Iterator of all the plugin_types
    records = ((k, v) for p in plugins for k, v in p.items())  # Squash them into a single dict
    collection_info = (r[1]['collection'] for r in records)  # Collection info for each plugin

    for collection in collection_info:
        fqcns_by_server[collection['source']].add(collection['fqcn'])

    with TemporaryDirectory() as tmpdir:
        download_dir = os.path.join(tmpdir, 'download')
        os.mkdir(download_dir)
        await fetch_collections(download_dir, fqcns_by_server)

        expanded_dir = os.path.join(tmpdir, 'expanded')
        os.mkdir(expanded_dir)
        await expand_collections(download_dir, expanded_dir)

        # Add the plugin information to the plugin_mapping
        await get_all_plugin_data(plugin_mapping, expanded_dir)
        # Write out the documentation files
        await write_docs(plugin_mapping, args.template_dir, args.output_dir)


#
# Subcommand incremental
#
def retrieve_collection_changes():
    """Retrieve changed collections from the galaxy server"""
    collection_map = {}
    page = '/api/v2/collections/?page_size={0}'.format(MAX_PAGE_SIZE)
    while page is not None:
        requestor = Request()
        response = requestor.get('https://{0}{1}'.format(GALAXY_SERVER, page))
        response_data = response.json()
        # Key by (namespace, collection name)
        collection_map.update({(rec['namespace']['name'], rec['name']):
                               rec['latest_version']['version']
                               for rec in response_data['results']})
        page = response_data["next"]

    return collection_map


def generate_incremental_docs(args):
    """Regenerate documentation for plugins whose collections have updated since the last build"""
    plugin_mapping = plugin_to_collection_mapping(args.plugin_to_collection_file)
    for plugins in plugin_mapping.values():
        pass
    # update_file_if_different(output_name, data)

    # Get the list of collections that we care about
    # Get the last generation time from the website
    # Get the changes since the last generation time
    # Get the files for the updated collections
    # Build docs for the plugins in the updated collections
    # Copy the new plugins into the tree
    # Update the last generation timestamp
    # Copy the new plugins into the tree

# How does this differ from the standard PluginDocs?
# Needs to be able to target per-directory
# Need to assemble from a combination of current collection plugin docs and the new thing that we
# create
#
# Also need to modify the current plugin docs generation so that it can point to these docs if these
# docs are going to exist


class CollectionPluginDocs(Command):
    name = 'document-collection-plugins'
    _ACTION_HELP = """Action to perform.
        validate: verifies the structure of the yaml file with the list of plugins.
        full: regenerate the documentation for all the plugins in the yaml file.
            This both updates the main website documentation and generates the new tree of
            collection documentation.
        incremental: regenerate the documentation for the plugins which have changed since the
            website was last updated (Not yet implemented)
    """

    @classmethod
    def init_parser(cls, add_parser):
        parser = add_parser(cls.name,
                            description='Generate documentation for plugins in collections.'
                            ' Plugins in collections will have a stub file in the normal plugin'
                            ' documentation location that says the module is in a collection and'
                            ' point to generated plugin documentation under the collections/'
                            ' hierarchy.')
        init_plugin_doc_arg_parser(parser)

        parser.add_argument('action', action='store', choices=('incremental', 'full', 'validate'),
                            default='full', help=cls._ACTION_HELP)
        parser.add_argument('--plugin-to-collection-file', action='store',
                            default=str(DEFAULT_PLUGIN_TO_COLLECTION_FILE),
                            help='Path to the file which lists the plugins to document which now'
                                 ' live in collections')
        parser.add_argument("-t", "--template-file", action="store", dest="template_file",
                            default=DEFAULT_PLUGIN_TEMPLATE_FILE,
                            help="Jinja2 template to use for the collectionized-plugins")
        parser.add_argument("--stub-template-file", action="store", dest="template_file",
                            default=DEFAULT_STUB_TEMPLATE_FILE,
                            help="Jinja2 template to point at the collectionized-plugin docs")

    @staticmethod
    def main(args):
        if not args.template_dir:
            args.template_dir = [os.path.abspath(str(DEFAULT_TEMPLATE_DIR))]
        if not args.output_dir:
            args.output_dir = os.path.abspath(str(DEFAULT_OUTPUT_DIR))

        loop = None
        if sys.version_info < (3, 7):
            # Event loop hasn't started yet, so don't use get_running_loop()
            loop = asyncio.get_event_loop()
            asyncio_run = loop.run_until_complete
        else:
            asyncio_run = asyncio.run

        if args.action == 'validate':
            try:
                validate(args)
            except Exception:
                print('{0} was *invalid*'.format(args.plugin_to_collection_file))
                raise
            else:
                print('{0} was *valid*'.format(args.plugin_to_collection_file))

        elif args.action == 'full':
            asyncio_run(generate_full_docs(args))

        else:
            # args.action == 'incremental' (Invalid actions are caught by argparse)
            generate_incremental_docs(args)

        return 0
