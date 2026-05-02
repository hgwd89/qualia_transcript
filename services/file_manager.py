import os
import config
from models.generated_file import GeneratedFile


def get_full_path(gf: GeneratedFile) -> str:
    return os.path.join(config.OUTPUT_DIR, gf.stored_path)


def file_exists(gf: GeneratedFile) -> bool:
    return os.path.isfile(get_full_path(gf))
