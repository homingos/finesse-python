"""Clone a voice from a reference clip and say something with it.

python examples/clone.py reference.wav "My voice"
"""

import sys

from flam_finesse import Finesse

client = Finesse()
voice = client.voices.clone_and_wait(
    name=sys.argv[2] if len(sys.argv) > 2 else "My voice",
    file=sys.argv[1],
    consent=True,  # you are the speaker, or hold their explicit permission
    on_update=lambda job: print("job:", job.status),
)
client.speech.create(voice_id=voice.id, text="This is my cloned voice.").save("clone.wav")
print("Voice", voice.id, "is ready; saved clone.wav")
