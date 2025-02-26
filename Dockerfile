# Auto bilibili live recording server
#
# VERSION               0.0.1
ARG COMMON_IMAGE=nvidia/cuda:11.0.3-devel-ubuntu20.04
FROM ${COMMON_IMAGE}

ENV TZ=Asia/Shanghai
ARG DEBIAN_FRONTEND=noninteractive


RUN rm -f /etc/apt/sources.list.d/cuda.list && \
    rm -f /etc/apt/sources.list.d/nvidia-ml.list && \
    which gpg && \
    (apt-key del 7fa2af80 && \
    curl -L -O https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/cuda-keyring_1.0-1_all.deb && \
    dpkg -i cuda-keyring_1.0-1_all.deb) || \
    echo "skip"
RUN apt-get update && apt-get install -y wget git apt-transport-https software-properties-common
RUN add-apt-repository universe
RUN apt-get update && apt-get install -y ffmpeg fonts-noto-color-emoji fonts-noto-cjk-extra cmake python3 python3-pip
RUN update-ca-certificates -f

RUN apt-get update && apt-get install -y libc6 libgcc1 libgssapi-krb5-2 libicu66 libssl1.1 libstdc++6 zlib1g

RUN wget https://github.com/PowerShell/PowerShell/releases/download/v7.1.5/powershell-7.1.5-linux-arm64.tar.gz -O powershell.tar.gz
RUN mkdir -p /opt/pwsh
RUN tar -xvzf powershell.tar.gz -C /opt/pwsh

RUN wget https://dot.net/v1/dotnet-install.sh
RUN bash ./dotnet-install.sh -c 6.0

RUN if [[ ${COMMON_IMAGE} == *"cuda"* ]] ; then ln -s /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1 ; fi

RUN ln -s /opt/pwsh/pwsh /usr/bin/powershell
RUN ln -s /root/.dotnet/dotnet /usr/bin/dotnet

RUN git clone https://github.com/BililiveRecorder/BililiveRecorder.git && cd BililiveRecorder && git checkout v2.5.0

WORKDIR "/BililiveRecorder"

RUN dpkgArch="$(uname -m)"; \
    case "$dpkgArch" in \
        aarch64) export RID='linux-arm64' ;; \
        x86_64) export RID='linux-x64' ;; \
        *) export RID='linux-x64' ;; \
    esac; \
    dotnet build BililiveRecorder.Cli/BililiveRecorder.Cli.csproj -r $RID -c Release -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:PublishTrimmed=True -p:TrimMode=Link &&\
    ln -s /BililiveRecorder/BililiveRecorder.Cli/bin/Release/net6.0/$RID/BililiveRecorder.Cli /BililiveRecorder/BililiveRecorder.Cli/bin/Release/net6.0/


RUN dotnet nuget locals all --clear
RUN rm -rf /opt/pwsh
RUN rm -rf /root/.dotnet


#ENTRYPOINT BililiveRecorder/BililiveRecorder.Cli/bin/Release/netcoreapp3.1/BililiveRecorder.Cli

WORKDIR "/"

RUN git clone https://github.com/hihkm/DanmakuFactory.git && cd DanmakuFactory && git checkout cab7cf813e5322ec3f41431fcae330a800d457a3

WORKDIR "/DanmakuFactory"

RUN mkdir temp
RUN make -f makefile

#ENTRYPOINT /bin/bash

RUN pip3 install git+https://github.com/PeterPanZH/danmaku_tools.git@1aa12c36f42e21795de5ec3a1c7cc3300c38082c

WORKDIR "/usr/local/bin"

RUN wget https://raw.githubusercontent.com/keylase/nvidia-patch/e87985e03ac2cf9b8e8086aa4b33a140f46fe036/patch.sh && \
    wget https://raw.githubusercontent.com/keylase/nvidia-patch/e87985e03ac2cf9b8e8086aa4b33a140f46fe036/docker-entrypoint.sh && \
    chmod +x patch.sh && \
    chmod +x docker-entrypoint.sh

WORKDIR "/webhook"

COPY requirements.txt .
RUN pip3 install --upgrade -r requirements.txt
RUN wget https://raw.githubusercontent.com/valkjsaaa/Bilibili-Toolkit/7b86a61214149cc3f790d02d5d06ecd7540b9bdb/bilibili.py

COPY *.py .

WORKDIR "/storage"
ENV PYTHONUNBUFFERED=1
CMD /usr/local/bin/docker-entrypoint.sh python3 -u /webhook/process_video.py
