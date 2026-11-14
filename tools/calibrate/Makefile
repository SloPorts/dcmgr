# Build with a KallistiOS environment loaded (source $KOS_BASE/environ.sh), then: make
TARGET = calib.elf
OBJS = calib.o
# JM-18: builtins and fast maths (stock KOS ships -fno-builtin)
KOS_CFLAGS += -fbuiltin -ffast-math -ffp-contract=fast

all: $(TARGET)

include $(KOS_BASE)/Makefile.rules

$(TARGET): $(OBJS)
	kos-cc -o $(TARGET) $(OBJS)
	$(KOS_STRIP) $(TARGET)
	# KOS embeds "<user>@<host>:<KOS_BASE>" and the build date in every program: drop them before sharing
	python3 scrub_banner.py $(TARGET)

clean:
	-rm -f $(TARGET) $(OBJS)
