#!/usr/bin/env bash

# This script assumes you've set up your account as follows
#sudo usermod -a -G adm,dialout,cdrom,sudo,dip,plugdev,lpadmin,lxd,sambashare <username>
sudo usermod -a -G adm,dialout,cdrom,sudo,dip,plugdev,lpadmin,lxd,sambashare kids

# First and foremost, set up firewall
# One can add any other firewall rules if so desired
sudo apt install ufw
sudo ufw allow ssh  # ssh
sudo ufw allow http  # http
sudo ufw allow https  # https
sudo ufw allow 5901:5910/tcp  # TCP ports
sudo ufw allow 6379  # redis port
sudo ufw allow from 128.111.237.0/24  # Physics
sudo ufw allow from 128.111.23.0/24  # Mazinlab
sudo ufw allow from 128.111.1.1  # UCSB DNS Server
sudo ufw allow from 128.111.1.2  # UCSB DNS Server
sudo ufw allow from 128.111.16.39  # Physics DNS Server
sudo ufw allow from 128.111.17.98  # Physics DNS Server

sudo ufw enable & sudo ufw reload  # Enable and reload the firewall with its new rules

# Install ssh
sudo apt install openssh-server  # This should automatically start the ssh server, and now you can ssh into the computer

# Install some necessary packages and set the default terminal to zsh
sudo apt install zsh vim nodejs curl git-all npm bison flex build-essential
sudo apt-get install -y make
sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"
touch ~/.Xauthority

# This progam will muck up FTDI USB-to-RS232 serial chips. Removing it gets rid of the issue
sudo apt-get remove brltty

# Install anaconda so that it is usable.
wget https://repo.anaconda.com/archive/Anaconda3-2020.11-Linux-x86_64.sh
chmod +x Anaconda-latest-Linux-x86_64.sh
bash Anaconda-latest-Linux-x86_64.sh
source ~/.zshrc  # Put the conda command on your path
conda config --add channels conda-forge
conda install pip
#conda install pip3

# Clone the MKID Control Repo and create the mkidcontrol environment
git clone https://github.com/MazinLab/mkidcontrol.git ~/mkidcontrol
cd ~/mkidcontrol
conda env create -f conda.yml

# Install redis server (NOTE: This will install the redis server, but will not configure it. The configuration will be
# done when installing the mkidcontrol repo and the custom redis config is moved to the proper location.)
sudo apt install redis-server

# Install redis (and a few useful links)
# https://redis.io/docs/stack/timeseries/quickstart/
# https://github.com/RedisTimeSeries/RedisTimeSeries
# https://github.com/redis/redis
git clone --recursive https://github.com/RedisTimeSeries/RedisTimeSeries.git
sudo cp -r RedisTimeSeries /
cd /RedisTimeSeries
make setup
make build
sudo cp bin/redistimeseries.so /usr/local/lib/redistimeseries.so

# Install redis-commander
sudo npm install -g redis-commander

pip3 install redistimeseries redis

# Make sure all necessary repositories are installed
#git clone https://github.com/MazinLab/mkidcore.git ~/src/mkidcore
#git clone https://github.com/MazinLab/mkidpipeline.git ~/src/mkidpipeline
#git clone https://github.com/MazinLab/mkidgen3.git ~/src/mkidgen3
#pip install -e ~/src/mkidcore
#pip install -e ~/src/mkidpipeline
#pip install -e ~/src/mkidgen3

# Install the different configuration necessities for mkidcontrol
cd /home/mazinlab/mkidcontrol
sudo cp etc/redis/redis.conf /etc/redis/
sudo cp etc/systemd/system/* /etc/systemd/system/
sudo cp etc/udev/rules.d/* /etc/udev/rules.d/
sudo cp etc/modules /etc/ # For the lakeshore240 and lakeshore372 cp210x USB to UART driver

# Now that repo is cloned and the environment is created and files are in place, activate env and install repo in it
conda activate control
pip install -e /home/mazinlab/mkidcontrol

# Load the udev rules and systemd services
# Prep all the systemd files that we loaded previously so they can be enabled/started
sudo systemctl daemon-reload

# Start redis server
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Compile cp210x.ko so that one can use all necessary usb devices. The following 2 lines have good source material for this
# https://community.silabs.com/s/question/0D51M00007xeNm8SAE/linux-cannot-identify-silab-serial-usb?language=en_US
# https://github.com/torvalds/linux/blob/master/drivers/usb/serial/cp210x.c
mkdir ~/original_ko_files
sudo cp /lib/modules/$(uname -r)/kernel/drivers/usb/serial/cp210x.ko ~/original_ko_files
# Currently this should be done manually following notes in mkidcontrol_notes.md / instructions here (which are in sync)
# Ensure that the cp210x.c file in this directory is for the proper linux kernel you've installed and you're using the
# version of gcc that you desire
cd ~/mkidcontrol/docs/hardware_reference_documentation/drivers/linuxlakeshoredriver
sudo make all # NOTE: IF THIS COMMAND FAILS, LOOK AT THE COMMAND THAT IS FIRST PRINTED AND THEN JUST RUN THAT
# INSTEAD (there's some weird path stuff going on). The command that worked for the xkid computer is below
#sudo make clean -C /lib/modules/`uname -r`/build M=/home/kids/mkidcontrol/docs/hardware_reference_documentation/drivers/lakeshoredriver/linuxlakeshoredriver modules
sudo cp cp210x.ko /lib/modules/$(uname -r)/kernel/drivers/usb/serial/
#insmod /lib/modules/$(uname -r)/kernel/drivers/usb/serial/usbserial.ko  # Will fail since this already exists
#insmod /lib/modules/$(uname -r)/kernel/drivers/usb/serial/cp210x.ko # Will also fail since the file already exists (can also just do insmod cp210x.ko)
sudo modprobe -r cp210x # Unload old
sudo modprobe cp210x # Reload new
# You can test this worked by running `lsmod | grep cp210x` to see if the module is running and also `modinfo cp210x` to get info about the module

# From https://indilib.org/individuals/devices/cameras/fli-ccd-filter-wheel.html
# How to install the drivers for the FLI (Finger Lakes Instruments) Filter Wheel for linux distro
sudo add-apt-repository ppa:mutlaqja/ppa
sudo apt-get update
sudo apt-get install indi-fli

# FLI SDK install + USB driver setup + Python Distro
# NOTE: This is for Linux-5.x.x kernels. If you move to 6 you may need to update the fliusb.c code for the driver
# The following will get the libfli.so that's needed to run FLI code in linux
wget https://www.flicamera.com/downloads/sdk/libfli-1.104.zip
unzip libfli-1.104.zip
git clone https://github.com/MazinLab/python-FLI.git
cp python-FLI/libfli.so-1.104_Makefile/Makefile libfli-1.104/
cd libfli-1.104
make clean
make
sudo cp libfli.so /usr/local/lib

# The following will install the python FLI library
cd ~/python-FLI
python setup.py install

# The following will create and install the FLI usb driver
# Create
cd ~
mkdir rts2fliusb
cd rts2fliusb
git clone https://github.com/MazinLab/fliusb.git
cd fliusb/fliusb
make clean
sudo make
# Install
sudo mkdir /lib/modules/5.15.0-41-generic/extra/
sudo cp fliusb.ko /lib/modules/5.15.0-41-generic/extra/
sudo insmod /usr/lib/modules/5.15.0-41-generic/extra/fliusb.ko
sudo depmod -a
sudo modprobe fliusb.ko

# Reload rules and trigger them to ensure drivers are running
sudo udevadm control --reload-rules
sudo udevadm trigger

# Get flask up and running to start (this can be added to the .zshrc file for ease and permanence)
# TODO: Get flask set up and running in a production FLASK_ENV
export FLASK_APP=/home/kids/mkidcontrol/mkidcontrol/controlflask/mkidDirector.py
export FLASK_ENV=develop # TODO: It may be good enough to just use FLASK_ENV=production instead of a whole WSGI setup
flask db init


cp ~/mkidcontrol/bin/mkid-service-control ~/.local/bin/
# Manually do the following
# sudo visudo
#add: kids  ALL=(ALL) NOPASSWD: /home/kids/.local/bin/mkid-service-control

# Start instrument software
sudo systemctl enable heatswitch
sudo systemctl start heatswitch

sudo systemctl enable lakeshore336
sudo systemctl start lakeshore336

sudo systemctl enable lakeshore372
sudo systemctl start lakeshore372

sudo systemctl enable lakeshore625
sudo systemctl start lakeshore625

# Start instrument control software
sudo systemctl enable controlflask
sudo systemctl start controlflask

# Reboot for anything further that needs to take effect
sudo reboot