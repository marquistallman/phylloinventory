try:
    import sounddevice as sd
    print("sounddevice:", sd.__version__)
    print("output devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            print("  [{}] {}  host={}  out={}".format(i, d["name"], d["hostapi"], d["max_output_channels"]))
    default = sd.query_devices(kind="output")
    print("default output:", default["name"])
except Exception as e:
    print("ERROR:", type(e).__name__, e)
