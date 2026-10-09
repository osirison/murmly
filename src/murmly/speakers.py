"""The speaker-embedding model Murmly uses to tell voices apart.

This module holds only the identity of the model file. `setup.sh` and
`bootstrap.ps1` download it from the URL below into the data directory and check
its SHA-256 against the value below; `tests/test_speakers_model_pin.py` fails
when either script drifts from these three constants.

The model is WeSpeaker ResNet34-LM trained on VoxCeleb, published by the
WeSpeaker project under CC BY 4.0 (`licenses/wespeaker-resnet34-lm-license.txt`).
The URL is pinned to one commit of the publisher's repository, so a later upload
cannot change what is fetched, and the checksum would refuse it if it did.
"""

from __future__ import annotations


SPEAKER_MODEL_URL = (
    "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/"
    "f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx"
)
SPEAKER_MODEL_FILENAME = "voxceleb_resnet34_LM.onnx"
SPEAKER_MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
