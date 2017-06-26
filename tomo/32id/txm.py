# -*- coding: utf-8 -*-

"""Defines a TXM class for controlling the Transmission X-ray
Microscope at Advanced Photon Source beamline 32-ID-C."""

import time
import math
import logging
import warnings
import asyncio
from contextlib import contextmanager

from epics import PV as EpicsPV, get_pv

import exceptions

DEFAULT_TIMEOUT = 20 # PV timeout in seconds

log = logging.getLogger(__name__)

class TxmPV(object):
    """A descriptor representing a process variable in the EPICS system.
    
    This allows accessing process variables as if they were object
    attributes. If the descriptor owner (ie. TXM) is not attached,
    this descriptor performs like a regular attribute. Optionally,
    this can also be done for objects that have no shutter permit.
    
    Attributes
    ----------
    put_complete : bool
      If False, there is a pending put operation.
    
    Parameters
    ----------
    pv_name : str
      The name of the process variable to connect to as defined in the
      EPICS system.
    dtype : optional
      If given, the values returned by `PV.get` will be
      typecast. Example: ``dtype=int`` will return
      ``int(PV[pv].get())``.
    default : optional
      If the owner is not attached, this value is returned instead of
      polling the instrument..
    permit_required : bool, optional
      If truthy, data will only be sent if the owner `has_permit`
      attribute is true. Reading of process variable is still enabled
      either way.
    wait : bool, optional
      If truthy, setting this descriptor will block until the
      operation succeeds.
    
    """
    _epicsPV = None
    put_complete = True
    
    def __init__(self, pv_name, dtype=None, default=None,
                 permit_required=False, wait=False, get_kwargs={}):
        # Make sure the dtype and float are compatible
        if dtype is not None:
            dtype(default)
        # Set default values
        self._namestring = pv_name
        self.curr_value = default
        self.dtype = dtype
        self.permit_required = permit_required
        self.wait = wait
        self.get_kwargs = get_kwargs
    
    def get_epics_PV(self, txm):
        # Only create a PV if one doesn't exist or the IOC prefix has changed
        is_cached = (self._epicsPV is not None and
                     self.ioc_prefix == txm.ioc_prefix)
        if not is_cached:
            self.ioc_prefix = txm.ioc_prefix
            pv_name = self.pv_name(txm)
            self._epicsPV = EpicsPV(pv_name)
        return self._epicsPV
    
    def pv_name(self, txm):
        """Do string formatting on the pv_name and return the result."""
        return self._namestring.format(ioc_prefix=txm.ioc_prefix)
    
    def __get__(self, txm, type=None):
        # Ask the PV for an updated value if possible
        if txm.is_attached:
            pv = self.get_epics_PV(txm)
            self.curr_value = pv.get(**self.get_kwargs)
        # Return the most recently retrieved value
        if self.dtype is not None:
            self.curr_value = self.dtype(self.curr_value)
        return self.curr_value
    
    def complete_put(self, txm):
        if txm.is_attached:
            is_not_done = True
        else:
            is_not_done = False
        while is_not_done and self.wait:
            time.sleep(0.01) # Give the PV a chance to settle
            curr_val = self.get_epics_PV(txm).get()
            is_not_done = (curr_val != self.curr_value)
        self.put_complete = True
    
    def __set__(self, txm, val):
        self.txm = txm
        # Check that the TXM has shutter permit if required for this PV
        if txm.is_attached and self.permit_required and not txm.has_permit:
            # There's a valid TXM but we can't operate this PV
            permit_clear = False
            msg = "PV {pv} was not set because TXM doesn't have permission."
            msg = msg.format(pv=self.pv_name)
            warnings.warn(msg)
        else:
            permit_clear = True
            self.curr_value = val
        # Set the PV (only if the TXM is attached and has permit)
        if txm.is_attached and permit_clear:
            pv = self.get_epics_PV(txm)
            self.put_complete = False
            txm.pv_queue.append(self)
            if self.wait:
                # Blocking version
                pv.put(val, wait=self.wait)
                self.complete_put()
            else:
                # Non-blocking version
                pv.put(val, callback=self.complete_put, callback_data=(txm,))
        elif not txm.is_attached:
            # Simulate a completed put call, because the callback isn't run
            self.put_complete = True
    
    def __set_name__(self, obj, name):
        self.name = name
    
    def __str__(self):
        return self.name

    def __repr__(self):
        return "<TxmPV: {}>".format(self._namestring)


def permit_required(real_func):
    """Decorates a method so it can only open with a permit.
    
    This method decorator ensures that the decorated method can only
    be called on an object that has a shutter permit. If it doesn't,
    then nothing happens.
    
    Parameters
    ----------
    return_value : optional
      Will be returned if the method is mocked.
    
    """
    def wrapped_func(obj, *args, **kwargs):
        # Inner function that checks the status of permit
        if obj.has_permit:
            ret = real_func(obj, *args, **kwargs)
        else:
            raise exceptions.PermitError()
        return ret
    return wrapped_func


class txm_required():
    """Decorates a method so it can only open with an instrument attached.
    
    This method decorator ensures that the decorated method can only
    be called on an object that has a real-world TXM instrument
    attached. If it doesn't, then nothing happens.
    
    Parameters
    ----------
    return_value
      Will be returned if the method is mocked.
    
    """
    def __init__(self, return_value):
        self.return_value = return_value
    
    def __call__(self, real_func):
        def wrapped_func(obj, *args, **kwargs):
            # Inner function that checks the status of permit
            if obj.is_attached:
                ret = real_func(obj, *args, **kwargs)
            else:
                ret = self.return_value
            return ret
        return wrapped_func


class TXM():
    """A class representing the Transmission X-ray Microscope at sector 32-ID-C.
    
    Attributes
    ----------
    is_attached : bool
      Is this computer able to communicate with the instrument. If
      False, communication methods will be simulated.
    has_permit : bool
      Is the instrument authorized to open shutters and change the
      X-ray source. Could be false for any number of reasons, most
      likely the beamline is set for hutch B to operate.
    ioc_prefix : str, optional
      The prefix to use for the camera's I/O controller when conneting
      certain PV's. PV descriptor's can then use "{ioc_prefix}" in
      their PV nam and have it format automatically.
    use_shutter_A : bool, optional
      Whether shutter A should be used when getting light.
    use_shutter_B : bool, optional
      Whether shutter B should be used when getting light.
    zp_diameter : float, optional
      The diameter (in nanometers) of the zone-plate currently
      installed in the instrument.
    drn : float, optional
      The width of the zoneplate's outermost diffraction zone.
    
    """
    pv_queue = []
    SHUTTER_OPEN = 0
    SHUTTER_CLOSED = 1
    E_RANGE = (5, 10) # How far can the X-ray energy be changed (in keV)
    POLL_INTERVAL = 0.01 # How often to check PV's in seconds.
    
    # Process variables
    # -----------------
    #
    # Detector PV's
    Cam1_ImageMode = TxmPV('{ioc_prefix}cam1:ImageMode')
    Cam1_ArrayCallbacks = TxmPV('{ioc_prefix}cam1:ArrayCallbacks')
    Cam1_AcquirePeriod = TxmPV('{ioc_prefix}cam1:AcquirePeriod')
    Cam1_TriggerMode = TxmPV('{ioc_prefix}cam1:TriggerMode')
    Cam1_SoftwareTrigger = TxmPV('{ioc_prefix}cam1:SoftwareTrigger')
    Cam1_AcquireTime = TxmPV('{ioc_prefix}cam1:AcquireTime')
    Cam1_FrameRateOnOff = TxmPV('{ioc_prefix}cam1:FrameRateOnOff')
    Cam1_FrameType = TxmPV('{ioc_prefix}cam1:FrameType')
    Cam1_NumImages = TxmPV('{ioc_prefix}cam1:NumImages')
    Cam1_Acquire = TxmPV('{ioc_prefix}cam1:Acquire')
    CCD_Motor = TxmPV('WHAT GOES HERE!?', default=500)
    
    # HDF5 writer PV's
    HDF1_AutoSave = TxmPV('{ioc_prefix}HDF1:AutoSave')
    HDF1_DeleteDriverFile = TxmPV('{ioc_prefix}HDF1:DeleteDriverFile')
    HDF1_EnableCallbacks = TxmPV('{ioc_prefix}HDF1:EnableCallbacks')
    HDF1_BlockingCallbacks = TxmPV('{ioc_prefix}HDF1:BlockingCallbacks')
    HDF1_FileWriteMode = TxmPV('{ioc_prefix}HDF1:FileWriteMode')
    HDF1_NumCapture = TxmPV('{ioc_prefix}HDF1:NumCapture')
    HDF1_Capture = TxmPV('{ioc_prefix}HDF1:Capture')
    HDF1_Capture_RBV = TxmPV('{ioc_prefix}HDF1:Capture_RBV')
    HDF1_FileName = TxmPV('{ioc_prefix}HDF1:FileName')
    HDF1_FullFileName_RBV = TxmPV('{ioc_prefix}HDF1:FullFileName_RBV',
                                  dtype=str, default='')
    HDF1_FileTemplate = TxmPV('{ioc_prefix}HDF1:FileTemplate')
    HDF1_ArrayPort = TxmPV('{ioc_prefix}HDF1:NDArrayPort')
    
    # Motor PV's
    Motor_SampleX = TxmPV('32idcTXM:mcs:c1:m2.VAL')
    Motor_SampleY = TxmPV('32idcTXM:xps:c1:m7.VAL')
    # Motor_SampleRot = TxmPV('32idcTXM:hydra:c0:m1.VAL')
    Motor_SampleRot = TxmPV('32idcTXM:ens:c1:m1.VAL')
    Motor_SampleZ = TxmPV('32idcTXM:mcs:c1:m1.VAL')
    Motor_X_Tile = TxmPV('32idc01:m33.VAL')
    Motor_Y_Tile = TxmPV('32idc02:m15.VAL')
    
    # Shutter PV's
    ShutterA_Open = TxmPV('32idb:rshtrA:Open', permit_required=True)
    ShutterA_Close = TxmPV('32idb:rshtrA:Close', permit_required=True)
    ShutterA_Move_Status = TxmPV('PB:32ID:STA_A_FES_CLSD_PL', default=0)
    ShutterB_Open = TxmPV('32idb:fbShutter:Open.PROC', permit_required=True)
    ShutterB_Close = TxmPV('32idb:fbShutter:Close.PROC', permit_required=True)
    ShutterB_Move_Status = TxmPV('PB:32ID:STA_B_SBS_CLSD_PL', default=0)
    ExternalShutter_Trigger = TxmPV('32idcTXM:shutCam:go', permit_required=True)
    
    # Fly macro PV's
    Fly_ScanDelta = TxmPV('32idcTXM:eFly:scanDelta')
    Fly_StartPos = TxmPV('32idcTXM:eFly:startPos')
    Fly_EndPos = TxmPV('32idcTXM:eFly:endPos')
    Fly_SlewSpeed = TxmPV('32idcTXM:eFly:slewSpeed')
    Fly_Taxi = TxmPV('32idcTXM:eFly:taxi')
    Fly_Run = TxmPV('32idcTXM:eFly:fly')
    Fly_ScanControl = TxmPV('32idcTXM:eFly:scanControl')
    Fly_Calc_Projections = TxmPV('32idcTXM:eFly:calcNumTriggers')
    
    # Theta control PV's
    Reset_Theta = TxmPV('32idcTXM:SG_RdCntr:reset.PROC')
    Proc_Theta = TxmPV('32idcTXM:SG_RdCntr:cVals.PROC')
    Theta_Array = TxmPV('32idcTXM:eFly:motorPos.AVAL')
    Theta_Cnt = TxmPV('32idcTXM:SG_RdCntr:aSub.VALB')
    
    # Misc PV's
    Image1_Callbacks = TxmPV('{ioc_prefix}image1:EnableCallbacks')
    ExternShutterExposure = TxmPV('32idcTXM:shutCam:tExpose')
    SetSoftGlueForStep = TxmPV('32idcTXM:SG3:MUX2-1_SEL_Signal')
    # ClearTheta = TxmPV('32idcTXM:recPV:PV1_clear')
    ExternShutterDelay = TxmPV('32idcTXM:shutCam:tDly')
    Interferometer = TxmPV('32idcTXM:SG2:UpDnCntr-1_COUNTS_s')
    Interferometer_Update = TxmPV('32idcTXM:SG2:UpDnCntr-1_COUNTS_SCAN.PROC')
    Interferometer_Reset = TxmPV('32idcTXM:SG_RdCntr:reset.PROC')
    Interferometer_Cnt = TxmPV('32idcTXM:SG_RdCntr:aSub.VALB')
    Interferometer_Arr = TxmPV('32idcTXM:SG_RdCntr:cVals.AA')
    Interferometer_Proc_Arr = TxmPV('32idcTXM:SG_RdCntr:cVals.PROC')
    Interferometer_Val = TxmPV('32idcTXM:userAve4.VAL')
    Interferometer_Mode = TxmPV('32idcTXM:userAve4_mode.VAL')
    Interferometer_Acquire = TxmPV('32idcTXM:userAve4_acquire.PROC')

    # Proc1 PV's
    Proc1_Callbacks = TxmPV('{ioc_prefix}Proc1:EnableCallbacks')
    Proc1_ArrayPort = TxmPV('{ioc_prefix}Proc1:NDArrayPort')
    Proc1_Filter_Enable = TxmPV('{ioc_prefix}Proc1:EnableFilter')
    Proc1_Filter_Type = TxmPV('{ioc_prefix}Proc1:FilterType')
    Proc1_Num_Filter = TxmPV('{ioc_prefix}Proc1:NumFilter')
    Proc1_Reset_Filter = TxmPV('{ioc_prefix}Proc1:ResetFilter')
    Proc1_AutoReset_Filter = TxmPV('{ioc_prefix}Proc1:AutoResetFilter')
    Proc1_Filter_Callbacks = TxmPV('{ioc_prefix}Proc1:FilterCallbacks')
    
    # TIFF writer PV's
    TIFF1_AutoSave = TxmPV('{ioc_prefix}TIFF1:AutoSave')
    TIFF1_DeleteDriverFile = TxmPV('{ioc_prefix}TIFF1:DeleteDriverFile')
    TIFF1_EnableCallbacks = TxmPV('{ioc_prefix}TIFF1:EnableCallbacks')
    TIFF1_BlockingCallbacks = TxmPV('{ioc_prefix}TIFF1:BlockingCallbacks')
    TIFF1_FileWriteMode = TxmPV('{ioc_prefix}TIFF1:FileWriteMode')
    TIFF1_NumCapture = TxmPV('{ioc_prefix}TIFF1:NumCapture')
    TIFF1_Capture = TxmPV('{ioc_prefix}TIFF1:Capture')
    TIFF1_FullFileName_RBV = TxmPV('{ioc_prefix}TIFF1:FullFileName_RBV')
    TIFF1_FileNumber = TxmPV('{ioc_prefix}TIFF1:FileNumber')
    TIFF1_FileName = TxmPV('{ioc_prefix}TIFF1:FileName')
    TIFF1_ArrayPort = TxmPV('{ioc_prefix}TIFF1:NDArrayPort')
    
    # Energy PV's
    DCMmvt = TxmPV('32ida:KohzuModeBO.VAL', permit_required=True)
    GAPputEnergy = TxmPV('32id:ID32us_energy', permit_required=True, wait=False)
    EnergyWait = TxmPV('ID32us:Busy')
    DCMputEnergy = TxmPV('32ida:BraggEAO.VAL', float, default=8.6, permit_required=True,)
    
    def __init__(self, has_permit=False, is_attached=True, ioc_prefix="",
                 use_shutter_A=False, use_shutter_B=False, zp_diameter=180,
                 drn=60):
        self.is_attached = is_attached
        self.has_permit = has_permit
        self.ioc_prefix = ioc_prefix
        self.use_shutter_A = use_shutter_A
        self.use_shutter_B = use_shutter_B
        self.zp_diameter = zp_diameter
        self.drn = drn
   
    @contextmanager
    def wait_pvs(self):
        """Context manager that allows for setting multiple PVS
        asynchronously.
        
        This manager creates an empty queue for PV objects. After
        exiting the inner code, it then waits until all the PV's are
        finished before returning.
        
        """
        # Make sure there are no pending PV changes
        self.flush_pvs()
        # Set up an event loop
        self.pv_queue = []
        # Return execution to the calling script
        yield None
        # Wait for all the PVs to be finished
        self.flush_pvs()
    
    def flush_pvs(self):
        """This method blocks until all the PV's in the pv_queue have reached
        their target values, then clears the queue.
        
        """
        while not all([pv.put_complete for pv in self.pv_queue]):
            time.sleep(0.01)
        # Reset the PV queue for next time
        log.debug("Flushed PV queue: %s", self.pv_queue)
        self.pv_queue = []
    
    def wait_pv(self, pv_name, target_val, timeout=DEFAULT_TIMEOUT):
        """Wait for a process variable to reach given value.
        
        This function polls the process variable (PV) and blocks until
        the PV reaches the target value or the max timeout, whichever
        comes first. This function immediately returns True if
        self.is_attached is False.
        
        Parameters
        ----------
        pv : str
          The process variable to be monitored, as defined by
          the pyepics system.
        target_val
          The value the PV should acquire before returning.
        timeout : int, optional
          How long to wait, in seconds, before giving up. Negative
          values cause the function to wait forever.
        
        Returns
        -------
        val : bool
          True if value was set properly, False if the timeout expired
          before the target value was reached.
        
        """
        log_msg = "called wait_pv({name}, {val}, timeout={timeout})"
        log.debug(log_msg.format(name=pv_name, val=target_val,
                                 timeout=timeout))
        # Delay for pv to change
        time.sleep(self.POLL_INTERVAL)
        startTime = time.time()
        # Enter into infinite loop polling the PV status
        while(True and self.is_attached):
            pv_val = getattr(self, pv_name)
            if (pv_val != target_val):
                if timeout > -1:
                    curTime = time.time()
                    diffTime = curTime - startTime
                    if diffTime >= timeout:
                        msg = "Timed out '{}' ({}) after {}s"
                        msg = msg.format(pv_name, target_val, timeout)
                        raise exceptions.TimeoutError(msg)
                        # log.debug("Timeout wait_pv()")
                        # return False
                time.sleep(.01)
            else:
                log.debug("Ended wait_pv()")
                return True
    
    def move_sample(self, x=None, y=None, z=None):
        """Move the sample to the given (x, y, z) position.

        This method is non-blocking.
        
        Parameters
        ----------
        x, y, z : float, optional
          The new position to move the sample to.
        
        """
        log.debug('Moving sample to (%s, %s, %s)', x, y, z)
        if x is not None:
            self.Motor_SampleX = float(x)
        if y is not None:
            self.Motor_SampleY = float(y)
        if z is not None:
            self.Motor_SampleZ = float(z)
        self.Motor_SampleRot = 0
        log.debug("Sample moved to (x=%s, y=%s, z=%s, θ=0°)", x, y, z)
        return True
    
    @permit_required
    def move_energy(self, energy, constant_mag=True, gap_offset=0.):
        """Change the energy of the X-ray source and optics.
        
        The undulator gap, monochromator, zone-plate and (optionally)
        detector will be moved.
        
        Parameters
        ----------
        energy : float
          The new energy (in kEV) for the X-ray source.
        constant_mag : bool, optional
          If truthy, the detector will also be moved to correct for
          the change in focal length.
        gap_offset : float, optional
          Extra energy to add to the value sent to the undulator gap.

        """
        # Check that the energy given is valid for this instrument
        in_range = self.E_RANGE[0] <= energy <= self.E_RANGE[1]
        if not in_range:
            msg = "Energy {energy} keV not in range {lower} - {upper} keV"
            msg = msg.format(energy=energy, lower=self.E_RANGE[0],
                             upper=self.E_RANGE[1])
            raise exceptions.EnergyError(msg)
        # Get the current values
        prev_energy = self.DCMputEnergy
        curr_CCD = self.CCD_Motor
        # Calculate target values
        landa = 1240.0 / (prev_energy * 1000.0)
        ZP_focal = self.zp_diameter * self.drn / (1000.0 * landa)
        inner =  math.sqrt(curr_CCD**2 - 4.0 * curr_CCD * ZP_focal)
        D = (curr_CCD + inner) / 2.0
        mag = (D - ZP_focal) / ZP_focal
        log.debug("Magnification: %.2f", mag)
        self.DCMmvt = 1
        landa = 1240.0 / (energy * 1000.0)
        ZP_focal = self.zp_diameter * self.drn / (1000.0 * landa)
        dist_ZP_ccd = mag * ZP_focal + ZP_focal
        ZP_WD = dist_ZP_ccd * ZP_focal / (dist_ZP_ccd - ZP_focal)
        new_CCD = ZP_WD + dist_ZP_ccd
        # Move the CCD to achieve constant magnification
        if constant_mag:
            log.debug("New CCD z-position: %f", new_CCD)
            self.CCD_Motor = new_CCD
        # Move the motors
        with self.wait_pvs():
            log.debug("New zoneplate z-position: %f", ZP_WD)
            self.ZpLocation = ZP_WD
            log.debug("New DCM Energy and Gap Energy: %f", energy)
            self.DCMputEnergy = energy
            self.GAPputEnergy = energy
        self.wait_pv('EnergyWait', 0, timeout=20)
        # Apply the offset to make sure the ID gap is correct
        with self.wait_pvs():
            self.GAPputEnergy = energy + gap_offset
        self.wait_pv('EnergyWait', 0, timeout=20)
        self.DCMmvt = 0
        
    
    def open_shutters(self):
        """Open the shutters to allow light in. The specific shutter(s) that
        opens depends on the values of ``self.use_shutter_A`` and
        ``self.use_shutter_B``.
        
        """
        if self.use_shutter_A:
            log.debug("Opening shutter A")
            self.ShutterA_Open = 1 # wait=True
            self.wait_pv('ShutterA_Move_Status', self.SHUTTER_OPEN)
        if self.use_shutter_B:
            log.debug("Opening shutter B")
            self.ShutterB_Open = 1
            self.wait_pv('ShutterB_Move_Status', self.SHUTTER_OPEN)
        # Display a logging info
        if self.use_shutter_A or self.use_shutter_B or not self.is_attached:
            log.info('Shutters opened.')
        else:
            warnings.warn("Neither shutter A nor B enabled.")

    def close_shutters(self):
        """Close the shutters to stop light in. The specific shutter(s) that
        closes depends on the values of ``self.use_shutter_A`` and
        ``self.use_shutter_B``.
        
        """
        if self.use_shutter_A:
            log.debug("Closing shutter A")
            self.ShutterA_Close = 1 # wait=True
            self.wait_pv('ShutterA_Move_Status', self.SHUTTER_CLOSED)
        if self.use_shutter_B:
            log.debug("Closing shutter B")
            self.ShutterB_Close = 1
            self.wait_pv('ShutterB_Move_Status', self.SHUTTER_CLOSED)
        # Display a logging info
        if self.use_shutter_A or self.use_shutter_B or not self.is_attached:
            log.info('Shutters closed.')
        else:
            warnings.warn("Neither shutter A nor B enabled.")
    
    def setup_writer(self, variableDict, filename=None):
        log.warning('setup_writer not implemented')
        return False
        """Prepare the HDF file writer to accept data.
        
        Parameters
        ----------
        variableDict : dict
          The arguments passed in by the calling GUI.
        filename : str, optional
          The name of the HDF file to save data to.
        
        """
        log.debug('setup_writer() called')
        if variableDict.has_key('Recursive_Filter_Enabled'):
            if variableDict['Recursive_Filter_Enabled'] == 1:
                # self.PVs['Proc1_Callbacks'].put('Disable')
                global_PVs['Proc1_Callbacks'].put('Enable')
                global_PVs['Proc1_Filter_Enable'].put('Disable')
                global_PVs['HDF1_ArrayPort'].put('PROC1')
                global_PVs['Proc1_Filter_Type'].put( Recursive_Filter_Type )
                n_images = int(variableDict['Recursive_Filter_N_Images'])
                global_PVs['Proc1_Num_Filter'].put(n_images)
                global_PVs['Proc1_Reset_Filter'].put( 1 )
                global_PVs['Proc1_AutoReset_Filter'].put( 'Yes' )
                global_PVs['Proc1_Filter_Callbacks'].put( 'Array N only' )
            else:
                # global_PVs['Proc1_Callbacks'].put('Disable')
                global_PVs['Proc1_Filter_Enable'].put('Disable')
                global_PVs['HDF1_ArrayPort'].put(global_PVs['Proc1_ArrayPort'].get())
        else:
            # global_PVs['Proc1_Callbacks'].put('Disable')
            global_PVs['Proc1_Filter_Enable'].put('Disable')
            global_PVs['HDF1_ArrayPort'].put(global_PVs['Proc1_ArrayPort'].get())
        global_PVs['HDF1_AutoSave'].put('Yes')
        global_PVs['HDF1_DeleteDriverFile'].put('No')
        global_PVs['HDF1_EnableCallbacks'].put('Enable')
        global_PVs['HDF1_BlockingCallbacks'].put('No')
        # Count total number of projections needed
        proj_vars = ['PreDarkImages', 'PreWhiteImages',
                     'PostDarkImages', 'PostWhiteImages']
        totalProj = 0
        for var in proj_vars:
            totalProj += int(variableDict.get(var, 0))
        # Add number for actual sample projections
        n_proj = int(variableDict.get('Projections', 0))
        proj_per_rot = int(variableDict.get('ProjectionsPerRot', 1))
        totalProj += n_proj * proj_per_rot
        global_PVs['HDF1_NumCapture'].put(totalProj)
        global_PVs['HDF1_FileWriteMode'].put(str(variableDict['FileWriteMode']), wait=True)
        if not filename == None:
            global_PVs['HDF1_FileName'].put(filename)
        global_PVs['HDF1_Capture'].put(1)
        wait_pv(global_PVs['HDF1_Capture'], 1)
        log.debug("Finished setting up HDF writer.")