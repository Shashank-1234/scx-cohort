# Build the P0.5 benchmark binaries. Usage: make -C src/bench [OUT=bin]
CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread
OUT ?= bin
all: $(OUT)/barrier $(OUT)/competitor $(OUT)/gangsim
$(OUT):
	mkdir -p $(OUT)
$(OUT)/barrier: barrier/barrier.c barrier/common.h | $(OUT)
	$(CC) $(CFLAGS) -o $@ barrier/barrier.c
$(OUT)/competitor: barrier/competitor.c barrier/common.h | $(OUT)
	$(CC) $(CFLAGS) -o $@ barrier/competitor.c
$(OUT)/gangsim: gangsim/gangsim.c | $(OUT)
	$(CC) $(CFLAGS) -o $@ gangsim/gangsim.c
clean:
	rm -rf $(OUT)
.PHONY: all clean
