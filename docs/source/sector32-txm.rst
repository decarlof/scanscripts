Sector 32-ID TXM
================

.. warning::

   This code is under active development and may change at any
   time. If you encounter issues, or documentation bugs, please
   `submit an issue`_.

This page describes the features of the :py:class:`tomo.32id.txm.TXM`
class, and a few supporting classes. The
:py:class:`~tomo.32id.txm.TXM` class is the primary interface for
controlling the Transmission X-ray Microscope (TXM) at beamline
32-ID-C.

A **core design goal** is to keep as much of the complexity in the
:class:`~tomo.32id.txm.TXM` class, which leaves the scripts to handle
high-level details. It also allows for better unit and integration
testing. When creating new scripts, it is recommended to **put all
interactions to process variables (PVs) in methods of the
:class:`~tomo.32id.txm.TXM` class**. This may seem silly for single PV
situations, but will make the script more readable. A hypothetical
example:

.. code:: python

   # Not readable at all: what does that address mean
   PV('32idcTXM:SG_RdCntr:reset.PROC').put(1, wait=True)
	  
   # Better, but still not great: what does 1 mean?
   txm.put_pv('Reset_Theta', 1)

   # Best, even though this method definition would only have one line
   txm.reset_theta()

Process Variables
-----------------

Process variables (PVs), though the :mod:`pyepics` package are the way
python controls the actuators and sensors of the instrument. There are
**two ways to interact with process variables**:

1. The :meth:`~tomo.32id.txm.TXM.pv_put` method on a
   :class:`~tomo.32id.txm.TXM` object.
2. A :class:`~tomo.32id.txm.TxmPV` descriptor on the
   :class:`~tomo.32id.txm.TXM` class (or subclass).

The second option is more efficient, but is easier to understand in
the context of the first option. The :meth:`~tomo.32id.txm.TXM.pv_put`
method is a wrapper around :meth:`pyepics.PV.put`, and accepts similar
arguments:

.. code:: python

   # These two sets of statements have the same effect

   # Using the epics PV class
   epics.PV('my_great_pv').put(1, wait=True)

   # Using the TXM method
   my_txm = TXM()
   my_txm.pv_put('my_great_pv', 1, wait=True)

Behind the scenes, there is some extra magic so :ref:`the txm can
coordinate PVs that work together <wait_pvs>`.

Manually supplying the PV name and options each time is cumbersome, so
the\ :py:class:`~tomo.32id.txm.txm_pv.TxmPV` descriptor can be used to
**define PVs at import time**. Set instances of the
:py:class:`~tomo.32id.txm.txm_pv.TxmPV` class as attributes on a
:class:`~tomo.32id.txm.TXM` subclass, then assign and retrieve values
directly from the attribute:

.. code:: python

   class ExampleTXM(TXM):
       # Define a PV during import time
       my_awesome_pv = TxmPV('cryptic:pv:string', dtype=float, wait=True)
       # More PV definitions go here

   # Now we can use the PV attribute of the txm class
   my_txm = ExampleTXM()
   # Retrieve the current value
   # Equivalent to ``float(epics.PV('cryptic:pv:string').get())``
   my_txm.my_awesome_pv
   # Set the value
   # Equivalent of epics.PV('cryptic:pv:string').put(2.718, wait=True)
   my_txm.my_awesome_pv = 2.718

The advantage here is that boilplate, such as type-casting and
blocking, can be defined once then forgotten. This approach also lets
you define PVs that should not be changed when the B-hutch is being
operated, by passing ``permit_required=True`` to the TxmPV
constructor. :ref:`More on this below <permits>`.

.. _wait_pvs:

Waiting on Process Variables
----------------------------

Sometimes it is necessary to set one PV then wait on a different PV to
confirm the new value. The :py:meth:`tomo.32id.txm.TXM.wait_pv` method
will poll a specified PV until it reaches its target value. It accepts
the *attribute name* of a PV, not the actual PV name itself. It may be
necessary to use the ``wait=False`` argument on the first PV to avoid
blocking forever:

.. code:: python

   class MyTXM(TXM):
       motor_pv = TxmPV('txm:motorA', wait=False
       sensor_pv = TxmPV('txm:sensorA')


   txm = MyTXM()
   # First set the actuator to the desired value
   new_position = 3.
   txm.motor_pv = new_position
   # This will block until the sensor reaches the target value
   tmx.wait_pv('sensor_pv', new_position)


Waiting on Multiple Process Variables
-------------------------------------

.. warning::

   This feature should be considered experimental. It has been know to
   break during some operations, most notably setting the undulator
   gap.

By default, calling the :py:meth:`~tomo.32id.txm.TXM.pv_put` method
will block execution until the ``put`` call has completed. This means
that setting several PVs becomes a serial operation. This is the
safest approach but is unnecessary in many situations. For example,
setting the x, y and z stage positions can be done simultaneously. You
can always use ``wait=False`` and handle the blocking yourself,
however this is not always straight-forward and may involve messy
callbacks. Using the :py:meth:`~tomo.32id.txm.TXM.wait_pvs` context
manager takes care of this. Any PVs that are set inside the context
will move immediately; if ``block=True`` (default) the manager will
wait for them to finish before leaving the context.

.. code:: python

    txm = TXM()

    # These move one at a time
    txm.Motor_SampleY = 5
    txm.Motor_SampleZ = 3

    # This waits while both motors move simultaneously
    with txm.wait_pvs():
        txm.Motor_SampleY = 8
	txm.Motor_SampleZ = 9

    # These move in the background without blocking
    with txm.wait_pvs(block=False):
        txm.Motor_SampleY = 3
	txm.Motor_SampleZ = 12

This table describes whether if and when a process variable blocks the
execution of python code and waits for the PV to achieve its target
value:

+---------------------------------+-----------------------+------------------------+
| Context manager                 | ``pv_put(wait=True)`` | ``pv_put(wait=False)`` |
+=================================+=======================+========================+
| No context                      | Blocks now            | No blocking            |
+---------------------------------+-----------------------+------------------------+
| ``TXM().wait_pvs``              | Blocks later          | No blocking            |
+---------------------------------+-----------------------+------------------------+
| ``TXM().wait_pvs(block=False)`` | No blocking           | No blocking            |
+---------------------------------+-----------------------+------------------------+

.. _permits:

Locking Shutter Permits
-----------------------

Sometimes it's desireable to test portions of the codebase during
downtime while the B-hutch is operating. In order to do this, however,
it's important to ensure that the shutters, undulator and
monochromator are not changed. Using the
:py:class:`~tomo.32id.txm_pv.TxmPV` descriptors makes this easy: any
PV's that should not be changed can be given the
``permit_required=True`` argument to their constructor:

.. code:: python

   class MyTXM(TXM):
       SHUTTER_OPEN = 1
       my_shutter = TxmPV('32idc:shutter', permit_required=True)
       
       def open_shutter(self):
           """Opens the shutter so we can science!"""
           self.my_shutter = self.SHUTTER_OPEN
   

   # This will not do anything
   my_txm = MyTXM()
   my_txm.open_shutter()

   # This will control the PV as expected
   my_txm = MyTXM(has_permit=True)
   my_txm.open_shutter()

.. note::

   There is no check that the C-hutch actually *has* permission to
   open the shutter, etc. It's controlled only by the ``has_permit``
   argument given to the :py:class:`~tomo.32id.txm.TXM`
   constructor. Please be considerate.

.. _submit an issue: https://github.com/tomography/scanscripts/issues
