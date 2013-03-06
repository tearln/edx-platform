"""Run a python process in a jail."""

# Instructions:
#   - AppArmor.md from xserver

import logging
import os, os.path
import resource
import shutil
import subprocess
import sys
import threading
import time

from .util import temp_directory

log = logging.getLogger(__name__)

# TODO: limit too much stdout data?

# Configure the Python command

PYTHON_CMD = None

def configure(python_bin, user=None):
    """Configure the jailpy module."""
    global PYTHON_CMD
    PYTHON_CMD = []
    if user:
        PYTHON_CMD.extend(['sudo', '-u', 'sandbox'])
    PYTHON_CMD.extend([python_bin, '-E'])

def is_configured():
    return bool(PYTHON_CMD)

# By default, look where our current Python is, and maybe there's a
# python-sandbox alongside.  Only do this if running in a virtualenv.
if hasattr(sys, 'real_prefix'):
    if os.path.isdir(sys.prefix + "-sandbox"):
        configure(sys.prefix + "-sandbox/bin/python", "sandbox")


class JailResult(object):
    """A passive object for us to return from jailpy."""
    pass

def jailpy(code, files=None, argv=None, stdin=None):
    """
    Run Python code in a jailed subprocess.

    `code` is a string containing the Python code to run.

    `files` is a list of file paths.

    Return an object with:

        .stdout: stdout of the program, a string
        .stderr: stderr of the program, a string
        .status: return status of the process: an int, 0 for successful

    """
    if not PYTHON_CMD:
        raise Exception("jailpy needs to be configured")

    with temp_directory(delete_when_done=True) as tmpdir:

        log.debug("Executing jailed code: %r", code)

        # All the supporting files are copied into our directory.
        for filename in files or ():
            if os.path.isfile(filename):
                shutil.copy(filename, tmpdir)
            else:
                dest = os.path.join(tmpdir, os.path.basename(filename))
                shutil.copytree(filename, dest)

        # Create the main file.
        with open(os.path.join(tmpdir, "jailed_code.py"), "w") as jailed:
            jailed.write(code)

        cmd = PYTHON_CMD + ['jailed_code.py'] + (argv or [])

        subproc = subprocess.Popen(
            cmd, preexec_fn=set_process_limits, cwd=tmpdir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        # TODO: time limiting

        killer = ProcessKillerThread(subproc)
        killer.start()
        result = JailResult()
        result.stdout, result.stderr = subproc.communicate(stdin)
        result.status = subproc.returncode

    return result


def set_process_limits():
    """
    Set limits on this processs, to be used first in a child process.
    """
    resource.setrlimit(resource.RLIMIT_CPU, (1, 1))     # 1 second of CPU--not wall clock time
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))   # no subprocesses
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))   # no files

    mem = 32 * 2**20     # 32 MB should be enough for anyone, right? :)
    resource.setrlimit(resource.RLIMIT_STACK, (mem, mem))
    resource.setrlimit(resource.RLIMIT_RSS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_DATA, (mem, mem))


class ProcessKillerThread(threading.Thread):
    def __init__(self, subproc, limit=1):
        super(ProcessKillerThread, self).__init__()
        self.subproc = subproc
        self.limit = limit

    def run(self):
        start = time.time()
        while (time.time() - start) < self.limit:
            time.sleep(.1)
            if self.subproc.poll() is not None:
                # Process ended, no need for us any more.
                return

        if self.subproc.poll() is None:
            # Can't use subproc.kill because we launched the subproc with sudo.
            killargs = ["sudo", "kill", "-9", str(self.subproc.pid)]
            kill = subprocess.Popen(killargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = kill.communicate()
            # TODO: This doesn't actually kill the process.... :(