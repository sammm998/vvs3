# Explicit build for the analysis service.
#
# A Dockerfile removes the deployment from the hands of a build-pack's
# language detection: the interpreter, the dependencies, the working directory
# and the listening port are all stated here, so what runs in the cloud is what
# was tested locally.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    VVS_STORAGE=/data/jobs

WORKDIR /app

# PyMuPDF, numpy, scipy, shapely and OpenCV ship wheels for this image, so no
# compiler is needed; libgomp is required by SciPy's threaded kernels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# main.py serves; worker.py is the child process it spawns for each analysis.
# Both are required - without the worker every upload fails at the moment the
# service tries to start it, which is exactly the failure this split was added
# to make impossible.
COPY main.py worker.py ./
COPY src ./src
COPY web ./web

RUN mkdir -p /data/jobs

EXPOSE 8080

# A container that cannot import the engine must fail here, not on the first
# upload half an hour later.
RUN python -c "import sys; sys.path.insert(0,'src/python'); import vvs_pipe, fitz, numpy, scipy, shapely, cv2; print('engine ok')" \
    && test -f worker.py && test -f web/index.html && test -f web/app.js && test -f web/app.css \
    && echo "service files ok"

CMD ["python", "main.py"]
