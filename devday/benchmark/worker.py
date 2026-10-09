"""One fresh process per trial; timeouts/crashes are captured by the runner."""
import argparse
import importlib
import random

from .data import read_json, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--observation", required=True)
    args = parser.parse_args()
    request = read_json(args.request)
    random.seed(request["seed"])
    import numpy as np
    np.random.seed(request["seed"])
    import cv2
    threads = request.get("execution", {}).get("opencv_threads")
    if threads is not None:
        cv2.setNumThreads(threads)
    module, name = args.adapter.split(":", 1)
    adapter = getattr(importlib.import_module(module), name)
    value = adapter(request)
    value["execution"] = {"opencv_threads": cv2.getNumThreads()}
    write_json(args.observation, value)


if __name__ == "__main__":
    main()
