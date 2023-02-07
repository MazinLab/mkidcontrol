from __future__ import print_function

import setuptools
import sys

import os
import platform
import subprocess

import numpy
import setuptools
import sys
from Cython.Build import cythonize
from setuptools.command.develop import develop
from setuptools.command.install import install
from setuptools.extension import Extension


MKIDSHM_DIR = 'mkidcontrol/packetmaster3/mkidshm'

def compile_and_install_software():
    """Used the subprocess module to compile/install the C software."""
    if 'linux' not in platform.system().lower():
        print('Not Linux, skipping compile/install of libmkidshm.so')
        return

    src_paths = ['./'+MKIDSHM_DIR]
    cmds = ["gcc -shared -o libmkidshm.so -fPIC mkidshm.c -lrt -lpthread"]

    def get_virtualenv_path():
        """Used to work out path to install compiled binaries to."""
        if hasattr(sys, 'real_prefix'):
            return sys.prefix

        if hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix:
            return sys.prefix

        if 'conda' in sys.prefix:
            return sys.prefix

        return None

    venv = get_virtualenv_path()

    try:
        for cmd, src_path in zip(cmds, src_paths):
            if venv:
                cmd += ' --prefix=' + os.path.abspath(venv)
            subprocess.check_call(cmd, cwd=src_path, shell=True)
    except Exception as e:
        print(str(e))
        raise e


class CustomInstall(install):
    """Custom handler for the 'install' command."""
    def run(self):
        compile_and_install_software()
        super(CustomInstall, self).run()


class CustomDevelop(develop):
    """Custom handler for the 'install' command."""
    def run(self):
        compile_and_install_software()
        super(CustomDevelop, self).run()


extensions = [Extension(name="mkidcontrol.packetmaster3.sharedmem",
                        sources=['mkidcontrol/packetmaster3/sharedmem.pyx'],
                        include_dirs=[numpy.get_include(), MKIDSHM_DIR],
                        extra_compile_args=['-shared', '-fPIC'],
                        library_dirs=[mkidshm_dir],
                        runtime_library_dirs=[os.path.abspath(MKIDSHM_DIR)],
                        extra_link_args=['-O3', '-lmkidshm', '-lrt', '-lpthread']),  # '-Wl',f'-rpath={MKIDSHM_DIR}']),
              Extension(name="mkidcontrol.packetmaster3.packetmaster",
                        sources=['mkidcontrol/packetmaster3/pmthreads.c', 'mkidcontrol/packetmaster3/packetmaster.pyx'],
                        include_dirs=[numpy.get_include(), 'mkidcontrol/packetmaster3/packetmaster', MKIDSHM_DIR],
                        library_dirs=[MKIDSHM_DIR],
                        runtime_library_dirs=[os.path.abspath(MKIDSHM_DIR)],
                        extra_compile_args=['-O3', '-shared', '-fPIC'],
                        extra_link_args=['-lmkidshm', '-lrt', '-lpthread'])
             ]


with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name='mkidcontrol',
    version='0.7.0',
    author='Noah Swimmer',
    author_email='nswimmer@ucsb.edu',
    description='MKID Instrument Control Software',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/MazinLab/mkidcontrol.git',
    packages=setuptools.find_packages(),
    # TODO may prove to be a major headache and we need to use either entry points or break the files into two parts
    #  with the script in bin/
    scripts=['mkidcontrol/agents/picturec/quenchAgent.py',
             'mkidcontrol/agents/xkid/heatswitchAgent.py',
             'mkidcontrol/agents/lakeshore240Agent.py',
             'mkidcontrol/agents/lakeshore336Agent.py',
             'mkidcontrol/agents/lakeshore372Agent.py',
             'mkidcontrol/agents/lakeshore625Agent.py',
             'mkidcontrol/agents/picturec/currentduinoAgent.py',
             'mkidcontrol/agents/picturec/hemttempAgent.py',
             'mkidcontrol/controlflask/mkidDirector.py',
             'mkidcontrol/agents/picturec/sim960Agent.py',
             'mkidcontrol/agents/picturec/sim921Agent.py',
             'mkidcontrol/agents/xkid/laserflipperAgent.py',
             'mkidcontrol/agents/xkid/focusAgent.py',
             'mkidcontrol/agents/xkid/filterwheelAgent.py',
             'mkidcontrol/agents/xkid/magnetAgent.py'],
    ext_modules=cythonize(extensions),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ]
)

#https://docs.python.org/3/distutils/setupscript.html#installing-package-data
