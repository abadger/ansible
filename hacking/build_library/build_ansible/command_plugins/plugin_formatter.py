# Copyright: (c) 2012, Jan-Piet Mens <jpmens () gmail.com>
# Copyright: (c) 2012-2014, Michael DeHaan <michael@ansible.com> and others
# Copyright: (c) 2017, Ansible Project

# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


import os
import pathlib
import re
import sys
from collections import defaultdict
from copy import deepcopy
from distutils.version import LooseVersion
from pprint import PrettyPrinter

import jinja2

from ansible.module_utils._text import to_bytes, to_native, to_text
from ansible.utils import plugin_docs
from ansible.utils.display import Display

# Pylint doesn't understand Python3 namespace modules.
# pylint: disable=relative-beyond-top-level
from ..change_detection import update_file_if_different
from ..commands import Command
from ..jinja2.environment import doc_environment
from ..document_plugins import (MalformedDocumentation, RemovedPlugin, get_plugin_info,
                                init_plugin_doc_arg_parser, normalize_plugin_info,
                                plugin_filename_format, plugin_output_directory)
# pylint: enable=relative-beyond-top-level


#####################################################################################
# constants and paths

DEFAULT_MODULE_DIR = pathlib.Path(__file__).parents[4] / 'lib/ansible/modules'
DEFAULT_TEMPLATE_DIR = pathlib.Path(__file__).parents[4] / 'docs/templates'

# The name of the DOCUMENTATION template
EXAMPLE_YAML = os.path.abspath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), os.pardir, 'examples', 'DOCUMENTATION.yml'
))

DEPRECATED = b" (D)"

pp = PrettyPrinter()
display = Display()


class InvalidArgument(Exception):
    """Problem parsing a command line argument"""


#
# General utilities
#

def validate_options(args):
    '''Convert and validate option parser options '''
    if not os.path.exists(args.module_dir):
        raise InvalidArgument("--module-dir, %s, does not exist" % args.module_dir)

    # Convert passed-in limit_to into list of modules.
    if args.limit_to is not None:
        args.limit_to = [s.lower() for s in args.limit_to.split(",")]

    if not args.template_dir:
        args.template_dir = os.path.abspath(DEFAULT_TEMPLATE_DIR)


def show_progress(progress):
    '''Show a little process indicator.'''
    if sys.stdout.isatty():
        sys.stdout.write('\r%s\r' % ("-/|\\"[progress % 4]))
        sys.stdout.flush()


def write_data(text, output_dir, outputname, module=None):
    ''' dumps module output to a file or the screen, as requested '''

    if output_dir is not None:
        if module:
            outputname = outputname % module

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        fname = os.path.join(output_dir, outputname)
        fname = fname.replace(".py", "")

        try:
            updated = update_file_if_different(fname, to_bytes(text))
        except Exception as e:
            display.display("while rendering %s, an error occured: %s" % (module, e))
            raise
        if updated:
            display.display("rendering: %s" % module)
    else:
        print(text)


#
# Setup
#

def get_template_map(template_dir, plugin_type):
    env = doc_environment(template_dir)

    templates = {}
    templates['plugin'] = env.get_template('plugin.rst.j2')
    templates['plugin_deprecation_stub'] = env.get_template('plugin_deprecation_stub.rst.j2')

    if plugin_type == 'module':
        name = 'modules'
    else:
        name = 'plugins'

    templates['category_list'] = env.get_template('%s_by_category.rst.j2' % name)
    templates['list_of_CATEGORY_modules'] = env.get_template('list_of_CATEGORY_%s.rst.j2'
                                                             % name)
    return templates


#
# Operate on plugins
#

def plugins_to_search(plugin_dir, limit_to=None):
    """
    Return the files which should be searched for plugin documentation

    :arg plugin_dir: The directory which is the toplevel location for modules
    :kwarg limit_to: Discard any modules which are not in this list
    :returns: iterator returning tuples of module (name, filename) for plugins in plugin_dir.
    """
    for root, _dummy, file_names in os.walk(plugin_dir):
        for filename in file_names:
            # Only search .py files for documentation:
            # * windows powershell modules have documentation stubs in python docstring
            #   format (they are not executed) so skip the ps1 format files
            if not filename.endswith('.py'):
                continue

            # Do not list __init__.py files
            if filename == '__init__.py':
                continue

            # Do not list blacklisted plugins
            plugin = os.path.splitext(filename)[0]
            if plugin in plugin_docs.BLACKLIST['MODULE'] or plugin == 'base':
                continue

            # If requested, limit plugin documentation building only to passed-in
            # plugins.
            if limit_to is not None and plugin.lower() not in limit_to:
                continue

            filename = os.path.join(root, filename)
            yield plugin, filename


def get_all_plugin_info(module_dir, limit_to=None):
    '''
    Returns information about plugins and the categories that they belong to

    :arg module_dir: file system path to the top of the plugin directory
    :kwarg limit_to: If given, this is a list of plugin names to
        generate information for.  All other plugins will be ignored.
    :returns: Tuple of two dicts containing plugin_info, categories, and
        aliases and a set listing deprecated modules:

        :plugin_info: mapping of module names to information about them.  The fields
            of the dict are:

            :path: filesystem path to the module
            :deprecated: boolean.  True means the module is deprecated otherwise not.
            :aliases: set of aliases to this module name
            :metadata: The modules metadata (as recorded in the module)
            :doc: The documentation structure for the module
            :seealso: The list of dictionaries with references to related subjects
            :examples: The module's examples
            :returndocs: The module's returndocs

        :categories: maps category names to a dict.  The dict contains at
            least one key, '_modules' which contains a list of module names in
            that category.  Any other keys in the dict are subcategories with
            the same structure.

    '''

    # Variables accumulating output
    categories = {}
    plugin_info = defaultdict(dict)

    plugin_index = 0
    for plugin_name, plugin_path in plugins_to_search(module_dir, limit_to=limit_to):
        plugin_index += 1
        show_progress(plugin_index)

        try:
            plugin_record = get_plugin_info(plugin_name, plugin_path, module_dir)
        except RemovedPlugin:
            # If the plugin was removed we simply skip the documentation
            continue
        except MalformedDocumentation as e:
            # Show an error and move on to the next
            display.error("*** ERROR: Malformed documentation: %s ***" % to_text(e))
            continue
        except Exception as e:
            # Unknown error.  We're going to continue to the next piece of documentation for this
            # too
            display.error("*** ERROR: Malformed documentation: %s ***" % to_text(e))
            continue

        # Use the plugin_name frpm plugin_record as it could have been normalized
        plugin_name = plugin_record.pop('plugin_name')

        if plugin_record['is_alias']:
            # use plugin_record to modify the actual plugin
            src_plugin = plugin_info[plugin_record['src_plugin']]
            if 'aliases' not in src_plugin:
                src_plugin['aliases'] = set()
            src_plugin['aliases'].add(plugin_name)

            # Continue to the next plugin because we only record this information alias information
            # in the source plugin.  There is not a seperate entry fpr the alias itself
            continue

        # Save the record to our combined return value

        if plugin_name in plugin_info:
            # This handles the case where an alias pointed at the plugin so
            # we already have a record of it that only contains alias information
            prior_aliases = plugin_info[plugin_name].get('aliases', set())
            plugin_record['aliases'] = prior_aliases
        else:
            plugin_record['aliases'] = set()

        plugin_info[plugin_name] = plugin_record

        # Get category information
        category = categories

        # Only include the paths inside of the library (ie: lib/ansible/modules)
        # as only the directory structure inside of there is considered a category
        mod_path_only = os.path.dirname(plugin_record['source'])

        module_categories = []
        # build up the categories that this module belongs to
        for new_cat in mod_path_only.split('/'):
            if new_cat not in category:
                category[new_cat] = {}
                category[new_cat]['_modules'] = []
            module_categories.append(new_cat)
            category = category[new_cat]

        category['_modules'].append(plugin_name)

    # keep module tests out of becoming module docs
    if 'test' in categories:
        del categories['test']

    return plugin_info, categories


def output_plugin_doc(doc, templates, outputname, output_dir):
    display.v('about to template %s' % doc['module'])
    display.vvvvv(pp.pformat(doc))
    try:
        text = templates['plugin'].render(doc)
    except Exception as e:
        display.warning(msg="Could not template %s due to %s" % (doc['module'], e))
        raise

    if LooseVersion(jinja2.__version__) < LooseVersion('2.10'):
        # jinja2 < 2.10's indent filter indents blank lines.  Cleanup
        text = re.sub(' +\n', '\n', text)

    write_data(text, output_dir, outputname, doc['module'])

    # Create deprecation stub pages for deprecated aliases
    for alias in doc['aliases']:
        doc['alias'] = alias

        display.v('about to template %s (deprecation alias %s)' % (doc['module'], alias))
        try:
            text = templates['plugin_deprecation_stub'].render(doc)
        except Exception as e:
            display.warning(msg="Could not template %s (deprecation alias %s) due to %s" %
                            (doc['module'], alias, e))
            raise

        if LooseVersion(jinja2.__version__) < LooseVersion('2.10'):
            # jinja2 < 2.10's indent filter indents blank lines.  Cleanup
            text = re.sub(' +\n', '\n', text)

        write_data(text, output_dir, outputname, alias)


def process_categories(plugin_info, categories, templates, output_dir, output_name, plugin_type):
    # For some reason, this line is changing plugin_info:
    # text = templates['list_of_CATEGORY_modules'].render(template_data)
    # To avoid that, make a deepcopy of the data.
    # We should track that down and fix it at some point in the future.
    plugin_info = deepcopy(plugin_info)
    for category in sorted(categories.keys()):
        module_map = categories[category]
        category_filename = output_name % category

        display.display("*** recording category %s in %s ***" % (category, category_filename))

        # start a new category file

        category_name = category.replace("_", " ")
        category_title = category_name.title()

        subcategories = dict((k, v) for k, v in module_map.items() if k != '_modules')
        template_data = {'title': category_title,
                         'category_name': category_name,
                         'category': module_map,
                         'subcategories': subcategories,
                         'module_info': plugin_info,
                         'plugin_type': plugin_type
                         }

        text = templates['list_of_CATEGORY_modules'].render(template_data)
        write_data(text, output_dir, category_filename)


def normalize_categories(categories, all_plugin_names):
    """
    Derive any information about categories that is needed

    :arg categories: Dictionary mapping category names to lists of modules
    :arg all_plugin_names: All of the named plugins

    .. warning:: This function operates by side-effect, modifying the categories dictionary
    """
    categories['all'] = {'_modules': all_plugin_names}


class DocumentPlugins(Command):
    name = 'document-plugins'

    @classmethod
    def init_parser(cls, add_parser):
        parser = add_parser(cls.name, description='Generate module documentation from metadata')
        init_plugin_doc_arg_parser(parser)
        parser.add_argument("-M", "--module-dir", action="store", dest="module_dir",
                            default=os.path.abspath(str(DEFAULT_MODULE_DIR)),
                            help="Ansible library path")

    @staticmethod
    def main(args):
        try:
            validate_options(args)
        except InvalidArgument as e:
            print(to_native(e), stream=sys.stderr)
            sys.exit(1)

        display.verbosity = args.verbosity
        plugin_type = args.plugin_type

        display.display("Evaluating %s files..." % plugin_type)

        # setup templating
        templates = get_template_map(args.template_dir, plugin_type)

        outputname = plugin_filename_format(plugin_type)
        output_dir = plugin_output_directory(plugin_type, args.output_dir)

        display.vv('output name: %s' % outputname)
        display.vv('output dir: %s' % output_dir)

        # Get the raw module info from the plugin file
        plugin_info, categories = get_all_plugin_info(args.module_dir, limit_to=args.limit_to)

        # Validate and transform the plugin data into a format suitable for templating
        plugin_documentation_data = {}
        for module_index, (plugin_name, record) in enumerate(plugin_info.items()):
            display.vv(plugin_name)
            show_progress(module_index)

            display.vvvvv(pp.pformat(('normalizing plugin doc: ', record['doc'])))

            collection = None
            collection_source = None
            normalize_plugin_info(record['doc'], plugin_name, collection, collection_source,
                                  record['source'], plugin_type, record['deprecated'],
                                  record['aliases'], record['metadata'], record['examples'],
                                  record['returndocs'])
            plugin_documentation_data[plugin_name] = record['doc']

            display.vvvvv(pp.pformat(record['doc']))

        del plugin_info

        # And validation and transformation for categories
        normalize_categories(categories, plugin_documentation_data.keys())

        #
        # Render all the individual plugin pages
        #
        display.v('Generating plugin pages')
        for doc in plugin_documentation_data.values():
            try:
                output_plugin_doc(doc, templates, outputname, output_dir)
            except Exception:
                # We ignore failed plugin docs :-(
                pass

        # Render all the categories for modules
        if plugin_type == 'module':
            display.v('Generating Category lists')
            category_list_name_template = 'list_of_%s_' + '%ss.rst' % plugin_type
            process_categories(plugin_documentation_data, categories, templates, output_dir,
                               category_list_name_template, plugin_type)

        if display.verbosity >= 3:
            display.vvv(pp.pformat(categories))
        if display.verbosity >= 5:
            display.vvvvv(pp.pformat(plugin_documentation_data))

        # Use the data to template the docs
        display.v('Generating rst for module categories')
        if plugin_type == 'module':
            display.v('Generating Categories')

            # Format module master category list
            category_list_text = templates['category_list'].render(
                categories=sorted(categories.keys()))
            category_index_name = '%ss_by_category.rst' % plugin_type

            # Write the category docs
            write_data(category_list_text, output_dir, category_index_name)

        return 0
