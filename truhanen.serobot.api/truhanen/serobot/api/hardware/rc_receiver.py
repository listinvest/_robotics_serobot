
import asyncio as aio
import time
from enum import Enum

from .bcm_channel import BcmChannel
from .gpio import GpioInput, GpioState


class RCCode(Enum):
    UP =     0x18
    DOWN =   0x52
    LEFT =   0x08
    RIGHT =  0x5a
    CENTER = 0x1c
    MINUS =  0x07
    PLUS =   0x15


class RCReceiver:
    def __init__(self):
        self._sensor = GpioInput(BcmChannel.remote_control_sensor)

    def get_code(self):
        """Read the signal sent by a remote control.

        TODO This is probably slow in Python.
            Implement in Cython or use some other method for
            reading the signal, e.g. a kernel module.

        Returns
        -------
        code : RCCode | 'repeat' | None
            The code received.
            Return an RCCode value if one could be decoded.
            Return 'repeat' if the previous code was repeated.
            Return None if no code could be decoded.
        """
        if self._sensor.state == GpioState.UNKNOWN:
            return None

        if self._sensor.state == GpioState.HIGH:
            return None

        # Catch the key initializer signal, 9 ms ON + 4.5 ms OFF.
        count = 0
        while self._sensor.state == GpioState.LOW and count < 200:
            count += 1
            time.sleep(.00006)
        if count < 10:
            # No initializer signal caught
            return None
        count = 0
        while self._sensor.state == GpioState.HIGH and count < 80:
            count += 1
            time.sleep(.00006)

        # Catch the key data, four bytes bit by bit.
        i_byte = 0
        i_bit = 0
        data = [0] * 4
        for _ in range(32):
            # Catch the bit initializer signal.
            count = 0
            while self._sensor.state == GpioState.LOW and count < 15:
                count += 1
                time.sleep(.00006)
            # The length of the next OFF period determines the bit.
            count = 0
            while self._sensor.state == GpioState.HIGH and count < 40:
                count += 1
                time.sleep(.00006)

            # Set the bit to data.
            if count > 7:
                data[i_byte] |= 1 << i_bit
            if i_bit == 7:
                i_bit = 0
                i_byte += 1
            else:
                i_bit += 1

        # Return the decoded key.
        if data[0] + data[1] == 255 and data[2] + data[3] == 255:
            try:
                code = RCCode(data[2])
            except ValueError:
                code = None
            return code
        else:
            return 'repeat'

    async def async_read_ir_remote_keys(self):
        """Iterator for yielding keys received by the IR remote sensor.

        TODO Create an asyncio.Task and put the keys to a asyncio.Queue.
        """
        while True:
            key = self.get_code()
            if key is not None:
                yield key
            await aio.sleep(.1)
