import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name='mkidcontrol',
    version='0.6.0',
    author='Noah Swimmer',
    author_email='nswimmer@ucsb.edu',
    description='MKID Instrument Control Software',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/MazinLab/mkidcontrol.git',
    packages=setuptools.find_packages(),
    # TODO may prove to be a major headache and we need to use either entry points or break the files into two parts
    #  with the script in bin/
    scripts=['mkidcontrol/quenchAgent.py',
             'mkidcontrol/heatswitchAgent.py',
             'mkidcontrol/lakeshore240Agent.py',
             'mkidcontrol/lakeshore336Agent.py',
             'mkidcontrol/lakeshore372Agent.py',
             'mkidcontrol/lakeshore625Agent.py',
             'mkidcontrol/currentduinoAgent.py',
             'mkidcontrol/hemttempAgent.py',
             'mkidcontrol/controlflask/mkidDirector.py',
             'mkidcontrol/sim960Agent.py',
             'mkidcontrol/sim921Agent.py'],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ]
)

#https://docs.python.org/3/distutils/setupscript.html#installing-package-data