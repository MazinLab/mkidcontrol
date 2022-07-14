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
sudo ufw allow

sudo ufw enable & sudo ufw reload  # Enable and reload the firewall with its new rules

# Install ssh
sudo apt install openssh-server  # This should automatically start the ssh server, and now you can ssh into the computer

# Install some necessary packages and set the default terminal to zsh
sudo apt install zsh vim nodejs curl git-all npm
sudo apt-get install -y make
sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"
touch ~/.Xauthority

# Install anaconda so that it is usable.
wget https://repo.anaconda.com/archive/Anaconda3-2020.11-Linux-x86_64.sh
chmod +x Anaconda-latest-Linux-x86_64.sh
bash Anaconda-latest-Linux-x86_64.sh
source ~/.zshrc  # Put the conda command on your path
conda config --add channels conda-forge

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
# STOPPED HERE UPON INITIAL INSTALL

# ------ EVERYTHING BELOW HERE IS LEGACY AND MUST BE VERIFIED ------

# This is obsolete because we will manually set up the redis server using our redis.service file (which somewhat mirrors
# the
# insert loadmodule /usr/local/lib/redistimeseries.so into /etc/redis/redis.conf
# sudo systemctl restart redis-server.service

#Clone this repo
git clone https://github.com/MazinLab/mkidcontrol.git ~/mkidcontrol

# Install anaconda and create the operating environment by running
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
cd /home/mazinlab/mkidcontrol
sudo cp etc/redis/redis.conf /etc/redis/
sudo cp etc/systemd/system/* /etc/systemd/system/
sudo cp etc/udev/rules.d/* /etc/udev/rules.d/
sudo cp etc/modules /etc/ # For the lakeshore240 and lakeshore372 driver

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