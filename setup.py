import setuptools

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
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ]
)

#https://docs.python.org/3/distutils/setupscript.html#installing-package-data