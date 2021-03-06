# Copyright 2014-2015 Facundo Batista, Nicolás Demarchi
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General
# Public License version 3, as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.
# If not, see <http://www.gnu.org/licenses/>.
#
# For further info, check  https://github.com/PyAr/fades

"""Main 'fades' modules."""

import argparse
import logging
import os
import platform
import signal
import sys
import shlex
import subprocess

import fades

from fades import FadesError
from fades import parsing, logger as fades_logger, cache, helpers, envbuilder, file_options

# the signals to redirect to the child process (note: only these are
# allowed in Windows, see 'signal' doc).
REDIRECTED_SIGNALS = [
    signal.SIGABRT,
    signal.SIGFPE,
    signal.SIGILL,
    signal.SIGINT,
    signal.SIGSEGV,
    signal.SIGTERM,
]

help_epilog = """
The "child program" is the script that fades will execute. It's an
optional parameter, it will be the first thing received by fades that
is not a parameter.  If no child program is indicated, a Python
interactive interpreter will be opened.

The "child options" (everything after the child program) are
parameters passed as is to the child program.
"""


def consolidate_dependencies(needs_ipython, child_program,
                             requirement_files, manual_dependencies):
    """Parse files, get deps and merge them. Deps read later overwrite those read earlier."""
    # We get the logger here because it's not defined at module level
    logger = logging.getLogger('fades')

    if needs_ipython:
        logger.debug("Adding ipython dependency because --ipython was detected")
        ipython_dep = parsing.parse_manual(['ipython'])
    else:
        ipython_dep = {}

    if child_program:
        srcfile_deps = parsing.parse_srcfile(child_program)
        logger.debug("Dependencies from source file: %s", srcfile_deps)
        docstring_deps = parsing.parse_docstring(child_program)
        logger.debug("Dependencies from docstrings: %s", docstring_deps)
    else:
        srcfile_deps = {}
        docstring_deps = {}

    all_dependencies = [ipython_dep, srcfile_deps, docstring_deps]

    if requirement_files is not None:
        for rf_path in requirement_files:
            rf_deps = parsing.parse_reqfile(rf_path)
            logger.debug('Dependencies from requirements file %r: %s', rf_path, rf_deps)
            all_dependencies.append(rf_deps)

    manual_deps = parsing.parse_manual(manual_dependencies)
    logger.debug("Dependencies from parameters: %s", manual_deps)
    all_dependencies.append(manual_deps)

    # Merge dependencies
    indicated_deps = {}
    for dep in all_dependencies:
        for repo, info in dep.items():
            indicated_deps.setdefault(repo, set()).update(info)

    return indicated_deps


def decide_child_program(args_executable, args_child_program):
    """Decide which the child program really is (if any)."""
    # We get the logger here because it's not defined at module level
    logger = logging.getLogger('fades')

    if args_executable:
        # if --exec given, check that it's just the executable name,
        # not absolute or relative paths
        if os.path.sep in args_child_program:
            logger.error(
                "The parameter to --exec must be a file name (to be found "
                "inside venv's bin directory), not a file path: %r",
                args_child_program)
            raise FadesError("File path given to --exec parameter")

        # indicated --execute, local and not analyzable for dependencies
        analyzable_child_program = None
        child_program = args_child_program
    elif args_child_program is not None:
        # normal case, the child program is to be analyzed (being it local or remote)
        if args_child_program.startswith(("http://", "https://")):
            args_child_program = helpers.download_remote_script(args_child_program)
        else:
            if not os.access(args_child_program, os.R_OK):
                logger.error("'%s' not found. If you want to run an executable "
                             "file from a library installed in the virtualenv "
                             "check the `--exec` option in the help.",
                             args_child_program)
                raise FadesError("child program  not found.")
        analyzable_child_program = args_child_program
        child_program = args_child_program
    else:
        # not indicated executable, not child program, "interpreter" mode
        analyzable_child_program = None
        child_program = None

    return analyzable_child_program, child_program


def detect_inside_virtualenv(prefix, real_prefix, base_prefix):
    """Tell if fades is running inside a virtualenv.

    The params 'real_prefix' and 'base_prefix' may be None.

    This is copied from pip code (slightly modified), see

        https://github.com/pypa/pip/blob/281eb61b09d87765d7c2b92f6982b3fe76ccb0af/
            pip/locations.py#L39
    """
    if real_prefix is not None:
        return True

    if base_prefix is None:
        return False

    # if prefix is different than base_prefix, it's a venv
    return prefix != base_prefix


def _get_normalized_args(parser):
    """Return the parsed command line arguments.

    Support the case when executed from a shebang, where all the
    parameters come in sys.argv[1] in a single string separated
    by spaces (in this case, the third parameter is what is being
    executed)
    """
    env = os.environ
    if '_' in env and env['_'] != sys.argv[0] and len(sys.argv) >= 1 and " " in sys.argv[1]:
        return parser.parse_args(shlex.split(sys.argv[1]) + sys.argv[2:])
    else:
        return parser.parse_args()


def go():
    """Make the magic happen."""
    parser = argparse.ArgumentParser(prog='PROG', epilog=help_epilog,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-V', '--version', action='store_true',
                        help="show version and info about the system, and exit")
    parser.add_argument('-v', '--verbose', action='store_true',
                        help="send all internal debugging lines to stderr, which may be very "
                             "useful to debug any problem that may arise.")
    parser.add_argument('-q', '--quiet', action='store_true',
                        help="don't show anything (unless it has a real problem), so the "
                             "original script stderr is not polluted at all.")
    parser.add_argument('-d', '--dependency', action='append',
                        help="specify dependencies through command line (this option can be "
                             "used multiple times)")
    parser.add_argument('-r', '--requirement', action='append',
                        help="indicate files to read dependencies from (this option can be "
                             "used multiple times)")
    parser.add_argument('-p', '--python', action='store',
                        help=("Specify the Python interpreter to use.\n"
                              " Default is: %s") % (sys.executable,))
    parser.add_argument('-x', '--exec', dest='executable', action='store_true',
                        help=("Indicate that the child_program should be looked up in the "
                              "virtualenv."))
    parser.add_argument('-i', '--ipython', action='store_true', help="use IPython shell.")
    parser.add_argument('--system-site-packages', action='store_true', default=False,
                        help=("Give the virtual environment access to the "
                              "system site-packages dir."))
    parser.add_argument('--virtualenv-options', action='append', default=[],
                        help=("Extra options to be supplied to virtualenv. (this option can be "
                              "used multiple times)"))
    parser.add_argument('--check-updates', action='store_true',
                        help=("check for packages updates"))
    parser.add_argument('--no-precheck-availability', action='store_true',
                        help=("Don't check if the packages exists in PyPI before actually try "
                              "to install them."))
    parser.add_argument('--pip-options', action='append', default=[],
                        help=("Extra options to be supplied to pip. (this option can be "
                              "used multiple times)"))
    parser.add_argument('--python-options', action='append', default=[],
                        help=("Extra options to be supplied to python. (this option can be "
                              "used multiple times)"))
    parser.add_argument('--rm', dest='remove', metavar='UUID',
                        help=("Remove a virtualenv by UUID. See --get-venv-dir option to "
                              "easily find out the UUID."))
    parser.add_argument('--clean-unused-venvs', action='store',
                        help=("This option remove venvs that haven't been used for more than "
                              "CLEAN_UNUSED_VENVS days. Appart from that, will compact usage "
                              "stats file.\n"
                              "When this option is present, the cleaning takes place at the "
                              "beginning of the execution."))
    parser.add_argument('--get-venv-dir', action='store_true',
                        help=("Show the virtualenv base directory (which includes the "
                              "virtualenv UUID) and quit."))
    parser.add_argument('child_program', nargs='?', default=None)
    parser.add_argument('child_options', nargs=argparse.REMAINDER)

    cli_args = _get_normalized_args(parser)

    # update args from config file (if needed).
    args = file_options.options_from_file(cli_args)

    # validate input, parameters, and support some special options
    if args.version:
        print("Running 'fades' version", fades.__version__)
        print("    Python:", sys.version_info)
        print("    System:", platform.platform())
        return 0

    # set up logger and dump basic version info
    logger = fades_logger.set_up(args.verbose, args.quiet)
    logger.debug("Running Python %s on %r", sys.version_info, platform.platform())
    logger.debug("Starting fades v. %s", fades.__version__)
    logger.debug("Arguments: %s", args)

    # verify that the module is NOT being used from a virtualenv
    if detect_inside_virtualenv(sys.prefix, getattr(sys, 'real_prefix', None),
                                getattr(sys, 'base_prefix', None)):
        logger.error(
            "fades is running from inside a virtualenv (%r), which is not supported", sys.prefix)
        raise FadesError("Cannot run from a virtualenv")

    if args.verbose and args.quiet:
        logger.warning("Overriding 'quiet' option ('verbose' also requested)")

    # start the virtualenvs manager
    venvscache = cache.VEnvsCache(os.path.join(helpers.get_basedir(), 'venvs.idx'))
    # start usage manager
    usage_manager = envbuilder.UsageManager(os.path.join(helpers.get_basedir(), 'usage_stats'),
                                            venvscache)

    if args.clean_unused_venvs:
        try:
            max_days_to_keep = int(args.clean_unused_venvs)
        except ValueError:
            logger.error("clean_unused_venvs must be an integer.")
            raise FadesError('clean_unused_venvs not an integer')

        usage_manager.clean_unused_venvs(max_days_to_keep)
        return 0

    uuid = args.remove
    if uuid:
        venv_data = venvscache.get_venv(uuid=uuid)
        if venv_data:
            # remove this venv from the cache
            env_path = venv_data.get('env_path')
            if env_path:
                envbuilder.destroy_venv(env_path, venvscache)
            else:
                logger.warning("Invalid 'env_path' found in virtualenv metadata: %r. "
                               "Not removing virtualenv.", env_path)
        else:
            logger.warning('No virtualenv found with uuid: %s.', uuid)
        return 0

    # decided which the child program really is
    analyzable_child_program, child_program = decide_child_program(
        args.executable, args.child_program)

    # Group and merge dependencies
    indicated_deps = consolidate_dependencies(args.ipython,
                                              analyzable_child_program,
                                              args.requirement,
                                              args.dependency)

    # Check for packages updates
    if args.check_updates:
        helpers.check_pypi_updates(indicated_deps)

    # get the interpreter version requested for the child_program
    interpreter, is_current = helpers.get_interpreter_version(args.python)

    # options
    pip_options = args.pip_options  # pip_options mustn't store.
    python_options = args.python_options
    options = {}
    options['pyvenv_options'] = []
    options['virtualenv_options'] = args.virtualenv_options
    if args.system_site_packages:
        options['virtualenv_options'].append("--system-site-packages")
        options['pyvenv_options'] = ["--system-site-packages"]

    create_venv = False
    venv_data = venvscache.get_venv(indicated_deps, interpreter, uuid, options)
    if venv_data:
        env_path = venv_data['env_path']
        # A venv was found in the cache check if its valid or re-generate it.
        if not os.path.exists(env_path):
            logger.warning("Missing directory (the virtualenv will be re-created): %r", env_path)
            venvscache.remove(env_path)
            create_venv = True
    else:
        create_venv = True

    if create_venv:
        # Check if the requested packages exists in pypi.
        if not args.no_precheck_availability and indicated_deps.get('pypi'):
            logger.info("Checking the availabilty of dependencies in PyPI. "
                        "You can use '--no-precheck-availability' to avoid it.")
            if not helpers.check_pypi_exists(indicated_deps):
                logger.error("An indicated dependency doesn't exist. Exiting")
                raise FadesError("Required dependency does not exist")

        # Create a new venv
        venv_data, installed = envbuilder.create_venv(indicated_deps, args.python, is_current,
                                                      options, pip_options)
        # store this new venv in the cache
        venvscache.store(installed, venv_data, interpreter, options)

    if args.get_venv_dir:
        # all it was requested is the virtualenv's path, show it and quit (don't run anything)
        print(venv_data['env_path'])
        return 0

    # run forest run!!
    python_exe = 'ipython' if args.ipython else 'python'
    python_exe = os.path.join(venv_data['env_bin_path'], python_exe)

    # add the virtualenv /bin path to the child PATH.
    environ_path = venv_data['env_bin_path']
    if 'PATH' in os.environ:
        environ_path += os.pathsep + os.environ['PATH']
    os.environ['PATH'] = environ_path

    # store usage information
    usage_manager.store_usage_stat(venv_data, venvscache)
    if child_program is None:
        interactive = True
        logger.debug(
            "Calling the interactive Python interpreter with arguments %r", python_options)
        cmd = [python_exe] + python_options
        p = subprocess.Popen(cmd)
    else:
        interactive = False
        if args.executable:
            cmd = [os.path.join(venv_data['env_bin_path'], child_program)]
            logger.debug("Calling child program %r with options %s",
                         child_program, args.child_options)
        else:
            cmd = [python_exe] + python_options + [child_program]
            logger.debug(
                "Calling Python interpreter with arguments %s to execute the child program"
                " %r with options %s", python_options, child_program, args.child_options)

        try:
            p = subprocess.Popen(cmd + args.child_options)
        except FileNotFoundError:
            logger.error("Command not found: %s", child_program)
            raise FadesError("Command not found")

    def _signal_handler(signum, _):
        """Handle signals received by parent process, send them to child.

        The only exception is CTRL-C, that is generated *from* the interactive
        interpreter (it's a keyboard combination!), so we swallow it for the
        interpreter to not see it twice.
        """
        if interactive and signum == signal.SIGINT:
            logger.debug("Swallowing signal %s", signum)
        else:
            logger.debug("Redirecting signal %s to child", signum)
            os.kill(p.pid, signum)

    # redirect the useful signals
    for s in REDIRECTED_SIGNALS:
        signal.signal(s, _signal_handler)

    # wait child to finish, end
    rc = p.wait()
    if rc:
        logger.debug("Child process not finished correctly: returncode=%d", rc)
    return rc
