'''
A simple metronome app built using tkinter, with audio processing handled 
using the sounddevice library. For a particular tempo, an array containing 
one bar worth of samples is created and a generator is used to perpetually 
supply chunks of samples to the sounddevice OutputStream via the callback 
function. In this way, it acts like a sliding window over the one-bar array.

A "drift error" accumulates over time as a result of representing one beat 
using an integer number of samples, thus discarding the fractional component 
of samples_per_beat. This drift error is monitored and corrected for, while 
the metronome is running.

Example:

    tempo = 145 bpm
    fs = 16000 Hz
    
    samples_per_beat = fs * 60.0 / tempo
                     = 16000 * 60.0 / 145
                     = 6620.6896
    
    drift_error_per_beat = samples_per_beat % 1

The drift error introduced per beat is 0.6896 samples. Once the cumulative
drift error exceeds 0.5 samples, the movement of the sliding window in the
generator is adjusted to compensate and keep the cumulative error in the
range [-0.5, 0.5] samples.

Changing tempo during playback is supported and the position within the bar 
is maintained across tempos without the bar restarting.

This is done by determining the fractional bar position, based on the output 
from the OutputStream, and determining the corresponding index in the new 
tempo's one-bar array. Playback resumes from this position at the new tempo.

This idea will need to be extended to allow for changing of time signature 
during playback.


Features to be implemented:
    * Handle time signature changes while running
    * Add alternative click sounds (and ability to change them while running?)
    * Enable scrolling to change tempo when hovering over tempo slider
    * Speed trainer
    * Add visual feedback to GUI for current beat (light-up bars or a number)
    * Investigate how to properly exit the tkinter process
    
    * DONE - Add buttons for +/- 5 bpm and +/- 10 bpm
    * DONE - Enable use of arrow keys for +/- 1 bpm
    * DONE - Enable use of space bar for start/stop
    * DONE - Add GUI support for different time signatures (temporarily disabled)
    * DONE - Drift error compensation
    
    
Problems to be solved:
    * Crashes: "Tcl_AsyncDelete: async handler deleted by the wrong thread"

'''

import queue
import sys
import threading
import collections

import librosa
import sounddevice as sd
import numpy as np

import tkinter as tk
from tkinter import Scale, Button, Frame, HORIZONTAL, DoubleVar
from PIL import ImageTk, Image

#%%

class Metronome():
    def __init__(self, master, tempo):
        
        self.running = False
        self.fs = 16000
        
        # Define some constants related to audio playback
        self.BLOCKSIZE = 256    # samples per frame of audio
        self.BUFFERSIZE = 40    # frames of audio to pre-fill queue with
        self.MAXQUEUESIZE = 50
        self.TIMEOUT = self.BLOCKSIZE * self.BUFFERSIZE / self.fs
        
        self.tempo = tempo
        self.q = queue.Queue(maxsize=self.MAXQUEUESIZE)
        self.event = threading.Event()
        
        # Set initial values for some metronome attributes
        self.beats_per_bar = 4
        self.samples_per_beat = None
        self.samples_per_bar = None
        self.get_seconds_per_bar()
        # Arrays to build the one-bar array
        self.zeros = None
        self.accent_beat = None
        self.non_accent_beat = None
        self.callback_frame_counter = 0
        
        self.tempo_start_bar_fraction = 0.0    # fractional progress through a bar at start of a tempo
        self.bar_fraction_at_tempo_change = 0.0
        
        # Drift error
        self.accumulated_drift_error = 0.0
        
        # Call methods for setup
        # Load samples from file
        self.hi, self.lo = self.load_click_samples()
        # Generate the bar and beat arrays
        self.generate_bar_and_beat_array(self.tempo)
        # Create a generator and an OutputStream
        self.gen = self.sample_generator()
        self.stream = self.create_stream()
        
        
        # Build the GUI components
        self.master = master
        master.title("Metronome")
        master.bind("<space>", lambda event: self.space_start_stop())
        self.tempo_scale_var = DoubleVar()
        
        #self.time_sig_var = tk.StringVar(value=str(self.beats_per_bar))
        #self.build_time_signature_frame()
        self.build_slider_frame()
        self.tempo_slider.set(self.tempo)
        self.build_start_stop_frame()
        
        
    ### Methods to load samples and build audio arrays
    def load_click_samples(self):
        hi, _ = librosa.load("./samples/hi.wav", sr=self.fs)
        lo, _ = librosa.load("./samples/lo.wav", sr=self.fs)
        return hi, lo
    
    
    def generate_bar_and_beat_array(self, tempo):
        self.samples_per_beat = int(self.fs * 60.0 / tempo)
        self.samples_per_bar = self.samples_per_beat * self.beats_per_bar
        self.zeros = np.zeros(self.samples_per_beat - len(self.hi))

        # Build arrays containing click sounds and the silence that follows
        self.accent_beat = np.concatenate([self.hi, self.zeros])
        self.non_accent_beat = np.concatenate([self.lo, self.zeros])

        # Build one bar of audio samples and an array with the beat number at every sample
        self.bar_array = np.concatenate([self.accent_beat, np.tile(self.non_accent_beat, self.beats_per_bar - 1)])
        self.beat_array = np.array([1+divmod(i, self.samples_per_beat)[0] for i in range(self.samples_per_bar)])
        
    
    def compute_drift_error_per_frame(self):
        '''
     
        '''
        
        samples_per_beat, decimal_component = divmod(self.fs * 60.0 / self.tempo, 1)
        error_per_sample = decimal_component / samples_per_beat
        
        return self.BLOCKSIZE * error_per_sample
    
    
    def get_seconds_per_bar(self):
        self.seconds_per_bar = self.beats_per_bar / (self.tempo / 60.0)
    
    
    def reset_counters(self):
        self.accumulated_drift_error = 0

    
    def apply_shift(self, shift_val):
        '''
        Use np.roll to shift self.bar_array and self.beat_array together.
        
        '''
        self.bar_array = np.roll(self.bar_array, shift_val)
        self.beat_array = np.roll(self.beat_array, shift_val)
        
    
    def map_current_position_to_new_tempo_index(self):
        '''
        Based on the current fractional progress through the bar at the
        current tempo, if a tempo change is instructed, calculcate the index
        at which the playback should start in the bar at the new tempo.
        
        For example, if the tempo is changed at some point during the second
        beat at tempo t1, we may have a fractional bar position of 0.34. We 
        want to maintain this position in the new self.bar array generated 
        for tempo t2, so that when the samples are sent to the stream, we
        start hearing the new tempo from the equivalent point during the
        second beat of the bar.
        
        This is a long way of saying that when we change the tempo, this 
        should map our position to the correct point in the bar and prevent
        the playback restarting on the first beat every time we change the tempo.
        '''
        
        # Once a tempo change has been instructed, we have a value for 
        # self.bar_fraction_at_tempo_change
        
        new_bar_fraction = (self.tempo_start_bar_fraction + self.bar_fraction_at_tempo_change) % 1
        bar_index_at_new_tempo = int(new_bar_fraction * self.samples_per_bar)
        print(f"New bar fraction: {new_bar_fraction:.4f}")
        
        # Update the fractional bar progress for new tempo
        self.tempo_start_bar_fraction = new_bar_fraction
        
        return bar_index_at_new_tempo
        
    
    def sample_generator(self):
        '''
        When given an array equivalent to one bar of click audio, yield
        the chunks of samples that will be passed to the sounddevice
        OutputStream instance. The arrays yielded from this generator will
        be assigned to "outdata" in the callback function referred to by
        OutputStream.        
        '''
        
        # Reset this counter each time a new generator instance is created
        self.accumulated_drift_error = 0
        # For the first output from the generator, yield the first set of samples
        yield self.bar_array[0:self.BLOCKSIZE], self.beat_array[0:self.BLOCKSIZE]
        
        frame_drift_error = self.compute_drift_error_per_frame()
        self.accumulated_drift_error += frame_drift_error
        
        # After yielding the initial set of samples, we yield further
        # chunks of samples indefinitely, from a generator instance
        while True:
            # Account for drift by adjusting roll value
            # Check if we need to roll the array to mitigate drift error
            if self.accumulated_drift_error >= 0.5:    # try to keep error in [-0.5, 0.5]
                # Get the integer part of the accumulated drift error  (or use 1 if [-0.5, 0.5])
                int_accumulated_drift_error = 1#int(self.accumulated_drift_error)
                # Adjust the accumulated_drift_error value to account for correction
                self.accumulated_drift_error -= int_accumulated_drift_error
                
                # This (self.samples_to_roll is just for debugging / testing)
                #self.samples_to_roll += int_accumulated_drift_error
                #print(self.samples_to_roll)
                
                roll_value = self.BLOCKSIZE - int_accumulated_drift_error
                
            else:
                roll_value = self.BLOCKSIZE
            
            
            # Shift the array by the required number of samples
            self.apply_shift(-roll_value)
            yield self.bar_array[0:self.BLOCKSIZE], self.beat_array[0:self.BLOCKSIZE]
            
            # Update accumulated drift error after each frame that is yielded
            frame_drift_error = self.compute_drift_error_per_frame()
            self.accumulated_drift_error += frame_drift_error
            #print(self.accumulated_drift_error)
            
            
    def set_tempo(self, tempo):
        '''
        Set the instance attribute self.tempo to a new value.
        Update the GUI components to reflect the change.
        
        Parameters
        ----------
        tempo (type: int)
            New tempo value (beats per minute) to use.
        
        
        Currently contains print outputs to aid development
        '''
        
        # Store the time that the old tempo ended, if currently running
        if self.running:
            self.tempo_end_time = self.stream.time
            self.time_at_tempo = self.tempo_end_time - self.tempo_start_time
            print(f"Time spent at tempo: {self.time_at_tempo:.3f} seconds")
            
            self.bars_at_tempo = self.time_at_tempo / self.seconds_per_bar
            self.bar_fraction_at_tempo_change = self.bars_at_tempo % 1
            print(f"Equivalent to {self.bars_at_tempo:.2f} bars")
            print(f"You were {self.bar_fraction_at_tempo_change:.4f} through the bar at tempo change\n")
        
        # Set the tempo instance attribute so the updated value is accessible by everything else
        self.tempo = int(tempo)
        
        # Reset accumulated drift error
        self.reset_counters()
        
        # Generate the bar and beat arrays
        self.generate_bar_and_beat_array(self.tempo)
        
        if self.running:
            # Map to correct position in new bar and perform the shift using np.roll
            new_bar_index = self.map_current_position_to_new_tempo_index()
            # Add some print statements for debugging
            print(f"The starting index in the new bar is {new_bar_index}")
            # Shift the newly generated bar and beat arrays to start at new_bar_idx
            self.apply_shift(-new_bar_index)
        
        # Create a new queue and fill it with new tempo samples
        self.create_and_fill_new_queue()
        
        # Record the time at which the new tempo started
        self.tempo_start_time = self.stream.time
        # Update self.seconds_per_bar for the new tempo
        self.get_seconds_per_bar()
        
        # TODO - Update the seven segment tempo display
        #self.update_seven_segment_tempo(self.tempo)
        # Update the slider position to reflect the new tempo
        self.tempo_scale_var.set(self.tempo)

    
    def adjust_tempo(self, tempo_adjustment):
        '''
        Adjusts the tempo by some value. The tempo_adjustment argument's value
        will come from the values associated with the arrow keys and GUI
        buttons for +/- 5 bpm and +/- 10 bpm.
        
        Check for adjustment causing the tempo to go out of range of the 
        tk.Scale self.tempo_scale. If out of range, pass the slider limit to
        self.set_tempo() to adjust self.tempo as well as the GUI components.
        
        '''
        
        if self.tempo + tempo_adjustment < int(self.tempo_slider.cget("from")):
            self.set_tempo(int(self.tempo_slider.cget("from")))
        
        elif self.tempo + tempo_adjustment > int(self.tempo_slider.cget("to")):
            self.set_tempo(int(self.tempo_slider.cget("to")))
   
        else:
            self.set_tempo(self.tempo + tempo_adjustment)
    
    
    def update_tempo_on_mouse_click_release(self, event):
        self.set_tempo(self.tempo_slider.get())


    ### Methods for audio playback
    def create_and_fill_new_queue(self):
        # Create empty queue and pre-fill it
        self.q = queue.Queue(maxsize=self.MAXQUEUESIZE)
        
        # Create a temporary generator to fill the queue. This avoids
        # the problem where the generator is already being executed in
        # self.callback
        temp_gen = self.sample_generator()
        
        # Get data from the generator and add it to queue
        for _ in range(self.BUFFERSIZE):
            data = next(temp_gen)
            if not len(data):
                break
            self.q.put_nowait(data)
            
        # Once the temporary generator is no longer being used to fill the queue,
        # replace self.gen with this temporary generator
        self.gen = temp_gen
        
        #print("\nFINISHED PRE-FILLING QUEUE\n")
    
    
    def create_stream(self):        
        # Create an OutputStream instance
        return sd.OutputStream(samplerate=self.fs,
                                     blocksize=self.BLOCKSIZE,
                                     channels=1,
                                     callback=self.callback,
                                     finished_callback=self.event.set)
    
    
    def callback(self, outdata, frames, time, status):
        #print("In Callback:", self.q.qsize())
        assert frames == self.BLOCKSIZE
        
        if status.output_underflow:
            print('Output underflow: increase blocksize?', file=sys.stderr)
            raise sd.CallbackAbort
        assert not status
        
        try:
            # Get the next data from the queue
            bar_data, beat_data = self.q.get_nowait()
        except queue.Empty as e:
            print('Buffer is empty: increase buffersize?', file=sys.stderr)
            raise sd.CallbackAbort from e
        
        if len(bar_data) < len(outdata):
            outdata[:len(bar_data)] = bar_data.reshape((-1,1))
            outdata[len(bar_data):].fill(0)
            raise sd.CallbackStop
        else:
            # Add the next data to the end of the queue
            try:
                data = next(self.gen)
                self.q.put(data, timeout=self.TIMEOUT)
                #print(self.q.qsize())
                # Send the data to the OutputStream
                outdata[:] = bar_data.reshape((-1,1))
                                
                # Debugging print statement - print out the current beat number
                #print(collections.Counter(beat_data).most_common()[0][0])
                #self.callback_frame_counter += 1
            except Exception as e:
                print(e)
            

    ### Methods for metronome controls
    def start(self):
        # Prevent multiple starts
        if self.running:
            return
        else:
            print("Starting...")
            self.running = True
            
            # Reset this because we want to go back to the beginning of a bar
            # each time we click start if the metronome is stopped
            self.tempo_start_bar_fraction = 0.0
            
            # Create new bar/beat arrays and a new generator, so we start at the beginning of the bar
            self.generate_bar_and_beat_array(self.tempo)
            self.reset_counters()
            self.gen = self.sample_generator()
            self.create_and_fill_new_queue()
            
            # Disable the beats per bar spinbox (for now)
            #self.time_signature_spinbox.config(state='disabled')
            
            # Start the stream (stream created in __init__)
            self.stream.start()
            # Take note of the time at which we started this particular tempo
            self.tempo_start_time = self.stream.time
            
            
    def stop(self):
        if not self.running:
            return
        else:
            print("Stopping...")
            self.running = False
            self.stream.abort()     # ends the stream quicker than stream.stop()
            # Re-enable the beats per bar spinbox (disabled for now)
            #self.time_signature_spinbox.config(state='readonly')
    
    
    def space_start_stop(self):
        '''
        Method to be bound to the space key so it can be used to start and stop
        the metronome.
        '''
        if self.running:
            self.stop()
        else:
            self.start()
            
   
    
    ### Methods for building the GUI components        
    def build_start_stop_frame(self):
        self.start_stop_button_frame = tk.Frame(master=self.master, width=400, pady=10, highlightbackground="gray", highlightthickness=0)
        self.start_stop_button_frame.pack()
        # Add the start and stop buttons
        self.start_button = tk.Button(self.start_stop_button_frame, width=20, height=3, text="START", command=self.start)        
        self.start_button.pack(fill=tk.Y, side='left', padx=0)
        self.stop_button = tk.Button(self.start_stop_button_frame, width=20, height=3, text="STOP", command=self.stop)
        self.stop_button.pack(fill=tk.Y, side='left', padx=0)
        
    
    def build_time_signature_frame(self):
        '''
        Currently not in use.
        '''
        self.time_signature_frame = tk.Frame(master=self.master)
        self.time_signature_label = tk.Label(master=self.time_signature_frame, text="Beats per bar:")
        self.time_signature_spinbox = tk.Spinbox(master=self.time_signature_frame,
                                                 from_=1,
                                                 to=16,
                                                 textvariable=self.time_sig_var,
                                                 command=self.update_beats_per_bar,
                                                 state='readonly')
        
        self.time_signature_frame.pack()
        self.time_signature_label.pack(side='left')
        self.time_signature_spinbox.pack(side='left')
        
        
    def update_beats_per_bar(self):
        '''
        Currently not in use.
        '''
        self.beats_per_bar = int(self.time_sig_var.get())
        print(self.beats_per_bar)
        
        
    def build_slider_frame(self):
        # A frame for the slider and tempo adjustment buttons
        self.tempo_slider_frame = tk.Frame(master=self.master, pady=5, highlightbackground="gray", highlightthickness=0)
        self.minus_10_button = tk.Button(self.tempo_slider_frame, width=4, text="-10", command=lambda:self.adjust_tempo(-10))
        self.minus_5_button = tk.Button(self.tempo_slider_frame, width=4, text="-5", command=lambda:self.adjust_tempo(-5))
        self.plus_5_button = tk.Button(self.tempo_slider_frame, width=4, text="+5", command=lambda:self.adjust_tempo(5))
        self.plus_10_button = tk.Button(self.tempo_slider_frame, width=4, text="+10", command=lambda:self.adjust_tempo(10))
               
        # Experimental - don't change tempo until mouse button is released from sliding the scale
        self.tempo_slider = tk.Scale(self.tempo_slider_frame, variable=self.tempo_scale_var, from_=40, to=300, length=400,
                                      width=50, showvalue=1, repeatdelay=1, sliderlength=50, resolution=1, orient=tk.HORIZONTAL,
                                      )#command=self.update_seven_segment_tempo)
        self.tempo_slider.bind("<ButtonRelease-1>", self.update_tempo_on_mouse_click_release)
        
        
        self.tempo_slider_frame.pack()
        self.minus_10_button.pack(fill=tk.Y, side='left', padx=0, pady=15)
        self.minus_5_button.pack(fill=tk.Y, side='left', padx=0, pady=15)
        self.tempo_slider.pack(fill=tk.Y, side='left', pady=10)
        self.plus_5_button.pack(fill=tk.Y, side='left', padx=0, pady=15)
        self.plus_10_button.pack(fill=tk.Y, side='left', padx=0, pady=15)
        
        # Try binding the arrow keys to increase/decrease the tempo
        self.master.bind('<Left>', lambda event: self.adjust_tempo(-10))
        self.master.bind('<Right>', lambda event: self.adjust_tempo(+10))
        self.master.bind('<Down>', lambda event: self.adjust_tempo(-1))
        self.master.bind('<Up>', lambda event: self.adjust_tempo(+1))

    

root = tk.Tk()
m = Metronome(master=root, tempo=170)
root.mainloop()

