"""Script for "punching out" qubit; i.e. initial estimates of readout 
resonator drive attenuation and frequency.

TODO:
    - integrate with cfg
    - see which params should be CL args
"""

import matplotlib.pyplot as plt
import numpy as np
import sys
import qubic.toolchain as tc
import qubic.run as rc

FBW = 6e6 
N_FREQ = 200
ATTEN_START = 12
ATTEN_STOP = 35
ATTEN_STEP = 1.0
N_SAMPLES = 100
RINGDOWN_TIME = 40.e-6
ACC_BUFSIZE = 1000

class PunchoutGUI:
    """
    Implements clickable GUI for selecting resonator power/freq
    """

    def __init__(self, freqs, attens, s11, qubitid=None):
        """
        Parameters
        ----------
            punchout : c_punchout object
                object containing punchout data to be plotted
            qubitid : str
                qubit identifier (todo: get this from punchout)
        """
        self.freq = None
        self.atten = None

        amp = np.abs(s11)
        phase = np.unwrap(np.angle(s11), axis=1)

        self.fig1 = plt.figure(figsize=(10, 10))
        self.sub = self.fig1.subplots(2, 2)
        self.fig1.suptitle(qubitid)
        self.sub[0, 0].pcolormesh(freqs, -attens, amp)
        self.sub[0, 1].pcolormesh(freqs, -attens, phase)
        self.sub[1, 0].pcolormesh(freqs[:-1], -attens, np.diff(amp, axis=1))
        self.sub[1, 1].pcolormesh(freqs[:-1], -attens, np.diff(phase, axis=1))
        self.fig1.canvas.mpl_connect('button_press_event', self.onClick)
        print('Click any plot to select desired resonator attenuation and frequency. If this is not a resonator, click outside the plot to remove from config')
        plt.show()

    def onClick(self, event):
        self.freq = event.xdata
        self.atten = event.ydata
        print('Selected resonator frequency {} and attenutation {}'.format(self.freq, self.atten))
        print('Click again to change, otherwise close')

class Punchout:

    def __init__(self, qchip, fpga_config, channel_configs, qubits=None, 
                 qubit_dict=None, freq_bandwidth=FBW, n_freq=N_FREQ, 
                 atten_start=ATTEN_START, atten_stop=ATTEN_STOP, atten_step=ATTEN_STEP):

        if n_freq > ACC_BUFSIZE:
            raise Exception('acc buf too small')

        if qubit_dict is None:
            assert qubits is not None
            qubit_dict = get_qubit_dict(qubits, qchip)

        self.chanmap = {qubit: channel_configs[qubit + '.rdlo'].core_ind for qubit in qubit_dict.keys()}

        freqoffs = np.linspace(-freq_bandwidth/2, freq_bandwidth/2, n_freq)
        self.attens = np.arange(atten_start, atten_stop, atten_step)
        self.freqs = {qubit: qubit_dict[qubit] + freqoffs for qubit in qubit_dict.keys()}

        circuits = self._make_punchout_circuits(qubit_dict, qchip, self.freqs, self.attens)
        compiled_progs = tc.run_compile_stage(circuits, fpga_config, qchip)
        self.raw_asm_progs = tc.run_assemble_stage(compiled_progs, channel_configs)
        

    def _make_punchout_circuits(self, qubit_dict, qchip, freqs, attens):
        circuits = []
        for atten in attens:
            amp = 10**(-atten/20)
            circuit = []
            for qubit in qubit_dict:
                for freq in freqs[qubit]:
                    circuit.append({'name': 'delay', 't': RINGDOWN_TIME, 'qubit': [qubit]})
                    circuit.append({'name': 'read', 'qubit': [qubit], 'modi': {(0, 'fcarrier'): freq, 
                                                                               (1, 'fcarrier'): freq,
                                                                               (0, 'amp'): amp}})
            circuits.append(circuit)
    
        return freqs, attens, circuits
    

    def run(self, circuit_runner, n_samples=N_SAMPLES):
        """
        Runs punchout sweep on selected qubit, plots results in clickable GUI. 
        TODO: this should also update configs.
    
        Parameters
        ----------
            qubitid : str
                qubit identifier
            fbw : float
                sweep bandwidth around initial res freq estimate
            n_freq : int
                number of freq points in sweep
            circuit_runner : CircuitRunner object
            atten_start, atten_stop, atten_step : int
                parameters for atten sweep
            n_samples : int
                ??
        """
    
        # compile first circuit and load all memory
        circuit_runner.load_circuit(self.raw_asm_progs[0])
    
        s11 = {qubit: np.zeros((len(self.attens), len(self.freqs)), dtype=np.complex128) for qubit in self.chanmap.keys()}
        nshot = ACC_BUFSIZE//len(self.freqs)
        navg = int(np.ceil(n_samples/nshot))
    
        for i, raw_asm in enumerate(self.raw_asm_progs):
            for core_ind in raw_asm.keys():
                circuit_runner.load_command_buf(core_ind, raw_asm[core_ind]['cmd_list'])
            iq_shots = circuit_runner.run_circuit(nshot, navg, len(self.freqs), delay=0.1)
            for qubit in self.chanmap.keys():
                s11[qubit][i] = np.average(np.reshape(iq_shots[self.chanmap[qubit]], (-1, len(self.freqs))), axis=0) 

        self.s11 = s11
    
     
    #for i in range(len(qubitids)):
    #    cal_gui = PunchoutGUI(punchout, i, qubitids[i])
    #    freq.append(cal_gui.freq)
    #    atten.append(cal_gui.atten)

    #return freq, atten, qubitids

def update_qchip(qchip, inst_cfg, freqs, attens, qubitids):
    for i, qubitid in enumerate(qubitids):
        qchip.qubits[qubitid].readfreq = freqs[i]
        amp = 10**((attens[i] + globalatten)/20)
        qchip.gates[qubitid + 'read'].contents[0].amp = amp

def get_qubit_dict(qubitids, qchip):
    """
    Helper function for returning punchout
    qubit dict from qchip config object.

    Parameters
    ----------
        qubitids : list
            list of qubit IDs to punch out ('Q0', 'Q1', etc)
        qchip : c_qchip object
            object containing qubit calibration config
    Returns
    -------
        dict
            keys: qubitids (from input)
            values: readout frequencies corresponding to each 
                    qubit, as specified by qchip
    """
    qubits = qchip.cfg_dict['Qubits']
    qubitdict = {}
    for k, v in qubits.items():
        if k in qubitids:
            qubitdict[k] = v['readfreq']

    return qubitdict

