import logging
import pyttsx3
import snoop

# Configure root logger
logging.basicConfig(
    level=logging.DEBUG,  # Enable detailed logs
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# Test pyttsx3 with espeak
logging.info("Starting text-to-speech conversion.")
engine = pyttsx3.init("espeak")
engine.say("Hello World 1")  # First spoken task
engine.say("Hello World 2")  # Second spoken task
engine.save_to_file("This is a file im saving", "output.wav")  # Save task

# Run and wait for all tasks to complete
engine.runAndWait()
logging.info("Text-to-speech conversion complete.")