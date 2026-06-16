from epics import caget, caput

ERRORFNAME = "error.txt"
STATUSPV = "EMPAD:cam1:Status"

with open(ERRORFNAME, "r") as f:
    lines = f.readlines()
    if len(lines) == 0:
        caput(STATUSPV, "python ioc exited succesfully")
    else:
        for line in lines:
            if "Error" in line:
                stripped = line.strip()
                caput(STATUSPV, stripped[:40])
                exit()
