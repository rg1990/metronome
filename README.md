# Python Metronome App

A simple metronome app built using *tkinter*, with audio processing handled using the *sounddevice* library. For a particular tempo, an array containing one bar worth of samples is created and a generator is used to perpetually supply chunks of samples to the *sounddevice* OutputStream via the callback function. In this way, it acts like a sliding window over the one-bar array.

A "drift error" accumulates over time as a result of representing one beat using an integer number of samples, thus discarding the fractional component of samples_per_beat. This drift error is monitored and corrected for, while the metronome is running.

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


Changing tempo during playback is supported and the position within the bar is maintained across tempos without the bar restarting.

This is done by determining the fractional bar position, based on the output from the OutputStream, and determining the corresponding index in the new tempo's one-bar array. Playback resumes from this position at the new tempo.

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
