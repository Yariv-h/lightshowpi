#!/usr/bin/env python
#
# Author: Todd Giles (todd.giles@gmail.com)
#
# Feel free to use, just send any enhancements back my way ;)
#
# Modifications By: Chris Usey (chris.usey@gmail.com)
# Modifications By: Ryan Jennings

"""Play any audio file and synchronize lights to the music

When executed, this script will play an audio file, as well as turn on and off 8 channels
of lights to the music (via the first 8 GPIO channels on the Rasberry Pi), based upon 
music it is playing. Many types of audio files are supported (see decoder.py below), but 
it has only been tested with wav and mp3 at the time of this writing.

The timing of the lights turning on and off are controlled based upon the frequency response
of the music being played.  A short segment of the music is analyzed via FFT to get the 
frequency response across 8 channels in the audio range.  Each light channel is then turned
on or off based upon whether the amplitude of the frequency response in the corresponding 
channel has crossed a dynamic threshold.

The threshold for each channel is "dynamic" in that it is adjusted upwards and downwards 
during the song playback based upon the frequency response amplitude of the song. This ensures
that soft songs, or even soft portions of songs will still turn all 8 channels on and off
during the song.

FFT caculation is quite CPU intensive and can adversely affect playback of songs (especially if
attempting to decode the song as well, as is the case for an mp3).  For this reason, the timing
values of the lights turning on and off is cached after it is calculated upon the first time a
new song is played.  The values are cached in a gzip'd text file in the same location as the
song itself.  Subsequent requests to play the same song will use the cached information and not
recompute the FFT, thus reducing CPU utilization dramatically and allowing for clear music 
playback of all audio file types.

Sample usage:

sudo python synchronized_lights.py --playlist=/home/pi/music/.playlist
sudo python synchronized_lights.py --file=/home/pi/music/jingle_bells.mp3

Third party dependencies:

alsaaudio: for audio output - http://pyalsaaudio.sourceforge.net/
decoder.py: decoding mp3, ogg, wma, and other audio files - https://pypi.python.org/pypi/decoder.py/1.5XB
numpy: for FFT calcuation - http://www.numpy.org/
raspberry-gpio-python: control GPIO output - https://code.google.com/p/raspberry-gpio-python/
wiringpi2: python wrapper around wiring pi - TODO(toddgiles): Add good link
"""

# Standard python imports
import argparse 
import ast
import csv
import fcntl
import gzip
import os
import random
from struct import unpack
import sys
import time
import wave

# Third party imports
import alsaaudio as aa
import decoder
import numpy as np
import wiringpi2 as wiringpi

# Local imports
import configuration_manager as cm
import hardware_controller as hc
import log as l



# Configurations - TODO(toddgiles): Move more of this into configuration manager
config = cm.config
limitlist = map(int,config.get('auto_tuning','limit_list').split(','))
limitthreshold = config.getfloat('auto_tuning','limit_threshold')
limitthresholdincrease = config.getfloat('auto_tuning','limit_threshold_increase')
limitthresholddecrease = config.getfloat('auto_tuning','limit_threshold_decrease')
maxoffcycles = config.getfloat('auto_tuning','max_off_cycles')
minfrequency = config.getfloat('audio_processing','min_frequency')
maxfrequency = config.getfloat('audio_processing','max_frequency')
randomizeplaylist = config.getboolean('lightshow','randomize_playlist')
try:
  customchannelmapping = map(int,config.get('audio_processing','custom_channel_mapping').split(','))
except:
  customchannelmapping = 0
try:
  customchannelfrequencies = map(int,config.get('audio_processing','custom_channel_frequencies').split(','))
except:
  customchannelfrequencies = 0
try:
  playlistpath = config.get('lightshow','playlist_path').replace('$SYNCHRONIZED_LIGHTS_HOME', cm.home_dir)
except:
  playlistpath  = "/home/pi/music/.playlist"
songtoplay = int(cm.get_state('song_to_play', 0))
play_now = int(cm.get_state('play_now', 0))

# Arguments
parser = argparse.ArgumentParser()
filegroup = parser.add_mutually_exclusive_group()
filegroup.add_argument('--playlist', default=playlistpath, help='Playlist to choose song from (see check_sms for details on format)')
filegroup.add_argument('--file', help='music file to play (required if no playlist designated)')
parser.add_argument('-v', '--verbosity', type=int, choices=[0, 1, 2], default=1, help='change output verbosity')
parser.add_argument('--readcache', type=int, default=1, help='read from the cache file. Default: true')
args = parser.parse_args()
l.verbosity = args.verbosity

# Make sure one of --playlist or --file was specified
if args.file == None and args.playlist == None:
    print "One of --playlist or --file must be specified"
    sys.exit()

# Initialize Lights
hc.SetPinsAsOutputs();

# Execute the "Preshow" for the given preshow configuration
def execute_preshow(config):
  for transition in config['transitions']:
    start = time.time()
    if transition['type'].lower() == 'on':
      hc.TurnOnLights(True)
    else:
      hc.TurnOffLights(True)
    l.log('Transition to ' + transition['type'] + ' for '
        + str(transition['duration']) + ' seconds', 2)
    while transition['duration'] > (time.time() - start):
      cm.load_state() # Force a refresh of state from file
      play_now = int(cm.get_state('play_now', 0))
      if play_now:
        return # Skip out on the rest of the preshow

      # Check once every ~ .1 seconds to break out
      time.sleep(0.1)

# Only execute preshow if no specific song has been requested to be played right now
if not play_now:
  execute_preshow(cm.lightshow()['preshow'])
      
# Determine the next file to play
file = args.file
if args.playlist != None and args.file == None:
    most_votes = [None, None, []]
    with open(args.playlist, 'rb') as f:
        fcntl.lockf(f, fcntl.LOCK_SH)
        playlist = csv.reader(f, delimiter='\t')
        songs = []
        for song in playlist:
            if len(song) < 2 or len(song) > 4:
                l.log('Invalid playlist', 0)
                sys.exit()
            elif len(song) == 2:
                song.append(set())
            else:
                song[2] = set(song[2].split(','))
                if len(song) == 3 and len(song[2]) >= len(most_votes[2]):
                    most_votes = song
            songs.append(song)
        fcntl.lockf(f, fcntl.LOCK_UN)

    if most_votes[0] != None:
        l.log("Most Votes: " + str(most_votes))
        file = most_votes[1]

        # Update playlist with latest votes
        with open(args.playlist, 'wb') as f:
            fcntl.lockf(f, fcntl.LOCK_EX)
            writer = csv.writer(f, delimiter='\t')
            for song in songs:
                if file == song[1] and len(song) == 3:
                    song.append("playing!")
                if len(song[2]) > 0:
                    song[2] = ",".join(song[2])
                else:
                    del song[2]
            writer.writerows(songs)
            fcntl.lockf(f, fcntl.LOCK_UN)

    else:
      # Get random song
      if randomizeplaylist:
        file = songs[random.randint(0, len(songs)-1)][1]
      # Get a "play now" requested song
      elif play_now > 0 and play_now <= len(songs):
        file = songs[play_now - 1][1]
      # Play next song in the lineup
      else:
        songtoplay = songtoplay if (songtoplay <= len(songs) - 1) else 0
        file = songs[songtoplay][1]
        nextsong = (songtoplay + 1) if ((songtoplay + 1) <= len(songs)-1) else 0
        cm.update_state('song_to_play', nextsong)

file = file.replace("$SYNCHRONIZED_LIGHTS_HOME", cm.home_dir)

# Ensure play_now is reset before beginning playback
if play_now:
  cm.update_state('play_now', 0)
  play_now = 0

# Initialize FFT stats
matrix    = [0 for i in range(hc.GPIOLEN)]
power     = []
offct     = [0 for i in range(hc.GPIOLEN)]

# Build the limit list
if len(limitlist) == 1:
  limit = [limitlist[0] for i in range(hc.GPIOLEN)]
else:
  limit = limitlist

# Set up audio
if file.endswith('.wav'):
   musicfile = wave.open(file, 'r')
else:
   musicfile = decoder.open(file)

sample_rate  = musicfile.getframerate()
no_channels  = musicfile.getnchannels()
chunk        = 4096 # Use a multiple of 8
output       = aa.PCM(aa.PCM_PLAYBACK, aa.PCM_NORMAL)
output.setchannels(no_channels)
output.setrate(sample_rate)
output.setformat(aa.PCM_FORMAT_S16_LE)
output.setperiodsize(chunk)

# Output a bit about what we're about to play
file = os.path.abspath(file)
l.log("Playing: " + file + " (" + str(musicfile.getnframes()/sample_rate) + " sec)", 0)

# Read in cached light control signals
cache = []
cache_found = False
cache_filename = os.path.dirname(file) + "/." + os.path.basename(file) + ".sync.gz"
try:
  with gzip.open(cache_filename, 'rb') as f:
    cachefile = csv.reader(f, delimiter=',')
    for row in cachefile:
      cache.append(row)
    cache_found = True
except IOError:
  l.log("Cached sync data file not found: '" + cache_filename + ".", 1)


# Return power array index corresponding to a particular frequency
def piff(val):
   return int(2*chunk*val/sample_rate)

#calculate frequency values for each channel
def calculate_channel_frequency(min_frequency, max_frequency, custom_channel_mapping, custom_channel_frequencies):
  # How many channels do we need to calculate the frequency for
  if custom_channel_mapping != 0 and len(custom_channel_mapping) == hc.GPIOLEN:
    l.log("Custom Channel Mapping is being used.",2)
    channelLength = max(custom_channel_mapping)
  else:
    l.log("Normal Channel Mapping is being used.",2)
    channelLength = hc.GPIOLEN
  
  l.log("Calculating frequencies for %d channels." % (channelLength), 2)
  octaves = (np.log(max_frequency/min_frequency))/np.log(2)
  l.log("octaves in selected frequency range ... %s" % octaves, 2)
  octaves_per_channel = octaves/channelLength
  frequency_limits = []
  frequency_store = []
  
  frequency_limits.append(min_frequency)
  if custom_channel_frequencies != 0 and (len(custom_channel_frequencies) >= channelLength + 1):
    l.log("Custom channel frequencies are being used",2)
    frequency_limits = custom_channel_frequencies
  else:
    l.log("Custom channel frequencies are not being used",2)
    for i in range(1, hc.GPIOLEN+1):
      frequency_limits.append(frequency_limits[-1]*10**(3/(10*(1/octaves_per_channel))))
  for i in range(0, channelLength):
    frequency_store.append((frequency_limits[i], frequency_limits[i+1]))
    l.log("channel %d is %6.2f to %6.2f " % (i, frequency_limits[i], frequency_limits[i+1]),2)
   
  # we have the frequencies now lets map them if custom mapping is defined
  if custom_channel_mapping != 0 and len(custom_channel_mapping) == hc.GPIOLEN:
    frequency_map=[]
    for i in range(0, hc.GPIOLEN):
      mapped_channel = custom_channel_mapping[i] -1
      mapped_frequency_set= frequency_store[mapped_channel]
      mapped_frequency_set_low= mapped_frequency_set[0]
      mapped_frequency_set_high= mapped_frequency_set[1]
      l.log("mapped channel: " + str(mapped_channel) + " will hold LOW: " + str(mapped_frequency_set_low) + ' HIGH: ' + str(mapped_frequency_set_high),2)
      frequency_map.append(mapped_frequency_set)
    return frequency_map
  else:
    return frequency_store


def calculate_levels(data, chunk, sample_rate, frequency_limits):
   global matrix
   # Convert raw data (ASCII string) to numpy array
   data = unpack("%dh"%(len(data)/2),data)
   data = np.array(data, dtype='h')
   
   # Apply FFT - real data
   fourier=np.fft.rfft(data)
   
   # Remove last element in array to make it the same size as chunk
   fourier=np.delete(fourier,len(fourier)-1)
   
   # Find average 'amplitude' for specific frequency ranges in Hz
   power = np.abs(fourier)   

   for i in range(hc.GPIOLEN):
      matrix[i] = np.mean(power[piff(frequency_limits[i][0]) :piff(frequency_limits[i][1]):1])

   # Tidy up column values for output to lights
   matrix=np.divide(matrix,100000)
   return matrix

# Process audio file
row = 0
data = musicfile.readframes(chunk)
frequency_limits = calculate_channel_frequency(minfrequency,maxfrequency,customchannelmapping,customchannelfrequencies)
while data != '' and not play_now:
   output.write(data)

   # Control lights with cached timing values if they exist
   if cache_found and args.readcache:
      if row < len(cache):
         entry = cache[row]
         for i in range (0,hc.GPIOLEN):
            if int(entry[i]): ## MAKE CHANGE HERE TO KEEP ON ALL THE TIME
               hc.TurnOnLight(i,True)
            else:
               hc.TurnOffLight(i,True)
      else:
         l.log("!!!! Ran out of cached timing values !!!!", 2)
         
   # No cache - Compute FFT from this chunk, and cache results
   else:
      entry = []
      matrix=calculate_levels(data, chunk, sample_rate, frequency_limits)
      for i in range (0,hc.GPIOLEN):
         if limit[i] < matrix[i] * limitthreshold:
            limit[i] = limit[i] * limitthresholdincrease
            l.log("++++ channel: {0}; limit: {1:.3f}".format(i, limit[i]), 2)
         # Amplitude has reached threshold
         if matrix[i] > limit[i]:
            hc.TurnOnLight(i,True)
            offct[i] = 0
            entry.append('1')
         else: # Amplitude did not reach threshold
            offct[i] = offct[i]+1
            if offct[i] > maxoffcycles:
               offct[i] = 0
               limit[i] = limit[i] * limitthresholddecrease # old value 0.8
            l.log("---- channel: {0}; limit: {1:.3f}".format(i, limit[i]), 2)
            hc.TurnOffLight(i,True)
            entry.append('0')
      cache.append(entry)

   # Read next chunk of data from music file
   data = musicfile.readframes(chunk)
   row = row + 1

   # Load new application state in case we've been interrupted
   cm.load_state()
   play_now = int(cm.get_state('play_now', 0))

if not cache_found:
   with gzip.open(cache_filename, 'wb') as f:
      writer = csv.writer(f, delimiter=',')
      writer.writerows(cache)
      l.log("Cached sync data written to '." + cache_filename + "' [" + str(len(cache)) + " rows]", 1)

# We're done, turn it all off ;)
hc.TurnOffLights()
hc.SetPinsAsInputs()