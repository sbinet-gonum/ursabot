FROM ppc64le-ubuntu-16.04-cpp

ADD requirements.txt requirements.txt
RUN pip install \
        -r requirements.txt

