#!/usr/bin/env python
# Basic energy calculation
import os
import numpy as np
from tcpb import TCProtobufClient as TCPBClient
import time


def wait_until_available(client: TCPBClient, max_wait=10.0, time_btw_check=1.0):
    total_wait = 0.0
    avail = False
    while not avail:
        try:
            client.connect()
            client.is_available()
            avail = True
        except:
            print(f'TeraChem server not available: \
                    trying again in {time_btw_check} seconds')
            time.sleep(time_btw_check)
            total_wait += time_btw_check
            if total_wait >= max_wait:
                raise TimeoutError('Maximum time allotted for checking for TeraChem server')
    print('Terachem Server is available')

def _print_times(times):
    total = 0.0
    print()
    print("Timings")
    print("-------------------------------")
    for key, value in times.items():
        print(f'{key:20s} {value:10.3f}')
        total += value
    print()
    print(f'{"total":20s} {total:10.3f}')
    print("-------------------------------")

def _val_or_iter(x):
    try:
        iter(x)
    except:
        x = [x]
    return x

def _convert(value):
    if isinstance(value, list):
        new = []
        for i in range(len(value)):
            new.append(_convert(value[i]))
        return new
    elif isinstance(value, bytes):
        return value.decode('utf-8')
    elif isinstance(value, np.ndarray):
        new = []
        for i in range(len(value)):
            new.append(_convert(value[i]))
        return new
    else:
        return value

def cleanup_job(results: dict, *remove: str):
    '''
        Removes unwanted entries in the TC output dictionary and converts
        all numpy arrays to lists
    '''
    #   
    if remove:
        if len(remove) == 1 and remove[0] == '':
            remove = []
    else:
        remove = ['charges', 'orb_energies']

    #   remove unwanted entries
    for r in remove:
        if r in results:
            results.pop(r)

    #   convert all numpy arrays to lists
    cleaned = {}
    for key in results:
        cleaned[key] = _convert(results[key])
        # if isinstance(results[key], np.ndarray):
        #     results[key] = results[key].tolist()
        # if isinstance(results[key], list) and len(results[key]):
        #     if isinstance(results[key][0], np.ndarray):
        #         results[key] = np.array(results[key]).tolist()

    return cleaned

class TCRunner():
    def __init__(self, host: str, port: int, atoms: list, tc_options: dict, run_options: dict={}, max_wait=20, dipole_deriv = False) -> None:

        #   set up the server
        self._client = client = TCPBClient(host=host, port=port)
        wait_until_available(client, max_wait=max_wait)


        self._atoms = np.copy(atoms)
        self._base_options = tc_options.copy()
        self._max_state = run_options.get('max_state', 0)
        self._grads = run_options.get('grads', [])
        self._NACs = run_options.pop('NACs', [])
        self._dipole_deriv = dipole_deriv

    def run_TC_single(client: TCPBClient, geom, atoms: list[str], opts: dict):
        opts['atoms'] = atoms
        start = time.time()
        results = client.compute_job_sync('energy', geom, 'angstrom', **opts)
        end = time.time()
        print(f"Job completed in {end - start: .2f} seconds")

        return results

    def run_TC_new_geom(self, geom):
        return self.run_TC_all_states(
            self._client, 
            geom, 
            self._atoms, 
            self._base_options, 
            self._dipole_deriv,
            self._max_state, 
            self._grads, 
            self._NACs)
    
    @staticmethod
    def run_TC_all_states(client: TCPBClient, geom, atoms: list[str], opts: dict, dipole_deriv=False,
            max_state:int=0, 
            grads:list[int]|int|str=[], 
            NACs:list[int]|float|str=[]):
        
        times = {}

        #   convert grads and NACs to list format 
        if isinstance(grads, str):
            if grads.lower() == 'all':
                grads = list(range(max_state+1))
            else:
                raise ValueError('grads must be an iterable of ints or "all"')
        else:
            grads = _val_or_iter(grads)
        if isinstance(NACs, str):
            if NACs.lower() == 'all':
                NACs = []
                for i in range(max_state+1):
                    for j in range(i+1, max_state+1):
                        NACs.append((i, j))
            else:
                raise ValueError('NACs must be an iterable in ints or "all"')
        else:
            NACs = _val_or_iter(NACs)

        #   make sure we are computing enough states
        base_options = opts.copy()
        if max_state > 0:
            base_options['cisrestart'] = 'cis_restart_' + str(os.getpid())
            if 'cisnumstates' not in base_options:
                base_options['cisnumstates'] = max_state + 2
            elif base_options['cisnumstates'] < max_state:
                raise ValueError('"cisnumstates" is less than requested electronic state')
        base_options['purify'] = False
        base_options['atoms'] = atoms
        
        #   run energy only if gradients and NACs are not requested
        all_results = []
        if len(grads) == 0 and len(NACs) == 0:
            job_opts = base_options.copy()
            start = time.time()
            results = client.compute_job_sync('energy', geom, 'angstrom', **job_opts)
            times[f'energy'] = time.time() - start
            results['run'] = 'energy'
            results.update(job_opts)
            all_results.append(results)

        #   gradient computations have to be separated from NACs
        for job_i, state in enumerate(grads):
            print("Grad ", job_i+1)
            job_opts = base_options.copy()
            if state > 0:
                job_opts['cis'] = 'yes'
                if 'cisnumstates' not in job_opts:
                    job_opts['cisnumstates'] = max_state + 2
                elif job_opts['cisnumstates'] < max_state:
                    raise ValueError('"cisnumstates" is less than requested electronic state')
                job_opts['cistarget'] = state

            if job_i > 0:
                job_opts['guess'] = all_results[-1]['orbfile']

            start = time.time()
            results = client.compute_job_sync('gradient', geom, 'angstrom', **job_opts)
            times[f'gradient_{state}'] = time.time() - start
            results['run'] = 'gradient'
            results.update(job_opts)
            all_results.append(results)

        #   run NAC jobs
        for job_i, (nac1, nac2) in enumerate(NACs):
            print("NAC ", job_i+1)
            job_opts = base_options.copy()
            job_opts['nacstate1'] = nac1
            job_opts['nacstate2'] = nac2
            job_opts['cis'] = 'yes'
            job_opts['cisnumstates'] = max_state + 2
            if dipole_deriv:
                pass
                # job_opts['cistransdipolederiv'] = 'yes'
            if job_i > 0:
                job_opts['guess'] = all_results[-1]['orbfile']

            start = time.time()
            results = client.compute_job_sync('coupling', geom, 'angstrom', **job_opts)
            times[f'nac_{nac1}_{nac2}'] = time.time() - start
            results['run'] = 'coupling'
            results.update(job_opts)
            all_results.append(results)


        _print_times(times)
        return all_results

def format_output_LSCIVR(n_elec, job_data: list[dict]):
    atoms = job_data[0]['atoms']
    n_atoms = len(atoms)
    energies = np.zeros(n_elec)
    grads = np.zeros((n_elec, n_atoms*3))
    nacs = np.zeros((n_elec, n_elec, n_atoms*3))
    
    for job in job_data:
        if job['run'] == 'gradient':
            state = job.get('cistarget', 0)
            grads[state] = np.array(job['gradient']).flatten()
            if isinstance(job['energy'], float):
                energies[state] = job['energy']
            else:
                energies[state] = job['energy'][state]
        elif job['run'] == 'coupling':
            state_1 = job['nacstate1']
            state_2 = job['nacstate2']
            nacs[state_1, state_2] = np.array(job['nacme']).flatten()
            nacs[state_2, state_1] = - nacs[state_1, state_2]

    return energies, grads, nacs