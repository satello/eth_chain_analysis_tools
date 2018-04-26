import sys
import csv
import bisect
import requests
import json
import time
import argparse
from threading import Thread
from queue import Queue

# constants
NUMBER_OF_HOLDERS = 1000000
BLOCK_NUMBER = 'eth_blockNumber'
GET_BALANCE = "eth_getBalance"
GET_BLOCK = "eth_getBlockByNumber"
URL = "{}:{}".format("http://localhost", 8545)
THREAD_COUNT = 50
CSV_NAME = 'top_addresses_%d.csv' % time.time()
# global variables
seen_addresses = {}
sorted_list = list()
task_queue = None
address_processing_queue = None
end_block = None
start_block = 0
current_estimate_block = 0
last_reported_block = 0
running = True
error = None

class Hodler:
    def __init__(self, address, balance):
        self.address = address
        self.balance = balance

    def __lt__(self, other):
        return self.balance < other.balance

    def __gt__(self, other):
        return self.balance > other.balance

    def __eq__(self, other):
        return self.balance == other.balance

    def as_list(self):
        return [self.address, self.balance]


def rpc_request(method, params = [], key = None):
    """Make an RPC request to geth on port 8545."""
    payload = {
        "method": method,
        "params": params,
        "jsonrpc": "2.0",
        "id": 0
    }

    res = requests.post(
          URL,
          data=json.dumps(payload),
          headers={"content-type": "application/json"}).json()

    if not res.get('result'):
        running = False
        error = res
        raise RuntimeError(res)
    return res['result'][key] if key else res['result']

# Queue the deletion and insertion of addresses so we don't run into any race conditions
def process_address_tuple():
    address_tuple = address_processing_queue.get()
    address = address_tuple[0]
    balance = address_tuple[1]
    if balance > 0 and (
        len(sorted_list) < NUMBER_OF_HOLDERS or balance > sorted_list[0].balance
    ):
        del sorted_list[0] # remove first item in list
        hodler = Hodler(address, balance) # create new hodler
        bisect.insort(sorted_list, hodler) # insert hodler
    address_processing_queue.task_done()

def process_block():
    while running:
        block_number = task_queue.get()
        current_estimate_block = block_number
        txs = rpc_request(method=GET_BLOCK, params=[hex(block_number), True], key='transactions')
        for tx in txs:
            # we consider an address active if it sent or received eth in the last year
            sender = tx["to"]
            reciever = tx["from"]
            # TODO check if contract 'eth_getCode'
            for addr in [sender, reciever]:
                if not addr:
                    continue
                if not seen_addresses.get(addr, None):
                    # We haven't seen this address yet, add to list
                    balance = int(rpc_request(method=GET_BALANCE, params=[addr, hex(end_block)]), 16)
                    seen_addresses[addr] = balance
                    # add to queue to process list writes and deletions on a single thread
                    address_processing_queue.put((addr, balance))
        task_queue.task_done()

# thread that reports on the progress every n seconds
def report_snapshot():
    sleep_time = 1800
    while running:
        # every half hour report on progress and write results in case of program failure
        print("Current Estimated block: %d" % current_estimate_block)
        print("Number of blocks processed since last snapshot: %d" % (current_estimate_block - last_reported_block))
        print("Running rate: %d blocks per second" % (float(current_estimate_block - last_reported_block) / sleep_time))
        print("Size of task queue: %d" % task_queue.qsize())
        print("Size of address queue: %d" % address_processing_queue.qsize())
        last_reported_block = current_estimate_block
        write_results_to_csv()

        time.sleep(sleep_time)

def write_results_to_csv():
    current_address_list = sorted_list.copy()
    address_csv = open(CSV_NAME, 'w')
    address_writer = csv.writer(address_csv, quoting=csv.QUOTE_ALL)
    address_writer.writerow([start_block])
    for hodler in reversed(current_address_list):
        address_writer.writerow(hodler.as_list())

def stop_queue_on_error():
    while running:
        pass

    # stop queue safely
    with task_queue.mutex:
        task_queue.queue.clear()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('-c', '--csv', required = False, help = 'Subscribers CSV to cross reference')
    ap.add_argument('-s', '--start', required = True, help = 'CSV of twitter followers screen names')
    ap.add_argument('-e', '--end', required = False, help = 'Last block. Will be used to check balance')

    args = vars(ap.parse_args())
    start_block = 0

    if not args['csv'] and not args['start']:
        raise RuntimeError("provide a start block (-s) or a csv (-c)")

    if args['csv']:
        with open(args['csv']) as f:
            reader = csv.reader(f)
            # now populate seen addresses and sorted list
            for row in reader:
                address = row[0]
                balance = int(row[1])

                seen_addresses[address] = balance
                sorted_list.insert(0, Hodler(address, balance))

    start_block = int(args['start'])

    if args['end']:
        end_block = int(args['end'])
    else:
        end_block = int(rpc_request(BLOCK_NUMBER, []), 16)

    # create task queue of size of all blocks
    task_queue = Queue(end_block - start_block)
    address_processing_queue = Queue(end_block - start_block)
    # set last_reported_block for first estimate
    last_reported_block = start_block
    # start threads

    # worker threads processing blocks (making rpc calls)
    for i in range(THREAD_COUNT):
        t = Thread(target=process_block)
        t.daemon = True
        t.start()

    # list maintanence thread
    list_worker = Thread(target=process_address_tuple)
    list_worker.daemon = True
    list_worker.start()

    # worker to stop threads if there is an error
    error_worker = Thread(target=stop_queue_on_error)
    error_worker.daemon = True
    error_worker.start()

    for i in range(start_block, end_block):
        # do the work
        task_queue.put(i)

    # thread for reporting
    reporter_thread = Thread(target=report_snapshot)
    reporter_thread.daemon = True
    reporter_thread.start()

    task_queue.join()

    if error:
        print("There was an error while processing the queue")
        print(error)
        print("estimated stopping point: %d" % current_estimate_block)

    # wait for all addresses to be processed
    address_processing_queue.join()
    write_results_to_csv()