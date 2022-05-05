#!/usr/bin/env bash

# This script assumes user accounts are setup as follows
#sudo usermod -a -G adm,dialout,cdrom,sudo,dip,plugdev,lpadmin,lxd,sambashare mazinlab

#Install Redis
#https://github.com/RedisTimeSeries/RedisTimeSeries
#https://oss.redislabs.com/redistimeseries/
#https://github.com/redis/redis



sudo apt install zsh vim nodejs
sudo npm install -g redis-commander
sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"
touch ~/.Xauthority

#Clone this repo
# git clone https://github.com/MazinLab/picturec.git ~/picturec

# Install anaconda and create the operating environment by running
# wget https://repo.anaconda.com/archive/Anaconda3-2020.11-Linux-x86_64.sh
# chmod +x Anaconda-latest-Linux-x86_64.sh
# bash Anaconda-latest-Linux-x86_64.sh
conda config --add channels conda-forge
cd ~/mkidcontrol
conda env create -f conda.yml

# Install dependencies and get computer ready for use

# Make sure all necessary repositories are installed
#git clone https://github.com/MazinLab/mkidcore.git ~/src/mkidcore
#git clone https://github.com/MazinLab/mkidpipeline.git ~/src/mkidpipeline
#git clone https://github.com/MazinLab/mkidgen3.git ~/src/mkidgen3
#pip install -e ~/src/mkidcore
#pip install -e ~/src/mkidpipeline
#pip install -e ~/src/mkidgen3

# Install the different configuration necessities for picturec
cd /home/mazinlab/picturec
sudo cp etc/redis/redis.conf /etc/redis/
sudo cp etc/systemd/system/* /etc/systemd/system/
sudo cp etc/udev/rules.d/* /etc/udev/rules.d/
sudo cp etc/modules /etc/ # For the lakeshore240 driver

# Load the udev rules and systemd services
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo systemctl daemon-reload

# Install the picturec repository
conda activate control
pip install -e /home/mazinlab/mkidcontrol

# Start redis server
sudo systemctl enable redis.service
sudo systemctl start redis.service

# Start instrument software
# Start hemtduino
sudo systemctl enable mkidcontrol
sudo systemctl start mkidcontrol

sudo reboot