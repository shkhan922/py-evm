class Compustate(object):
    memory = None
    stack = None
    gas = None

    def __init__(self):
        self.memory = []
        self.stack = []
        self.gas = 0


def vm_execute(ext, msg, code):
    # precompute trace flag
    # if we trace vm, we're in slow mode anyway
    trace_vm = log_vm_op.is_active('trace')

    compustate = Compustate(gas=msg.gas)
    stk = compustate.stack
    mem = compustate.memory

    if code in code_cache:
        processed_code = code_cache[code]
    else:
        processed_code = preprocess_code(code)
        code_cache[code] = processed_code

    codelen = len(processed_code)

    op = None
    steps = 0
    _prevop = None  # for trace only

    while 1:
        # stack size limit error
        if compustate.pc >= codelen:
            return peaceful_exit('CODE OUT OF RANGE', compustate.gas, [])

        op, in_args, out_args, fee, opcode, pushval = \
            processed_code[compustate.pc]

        # out of gas error
        if fee > compustate.gas:
            return vm_exception('OUT OF GAS')

        # empty stack error
        if in_args > len(compustate.stack):
            return vm_exception('INSUFFICIENT STACK',
                                op=op, needed=to_string(in_args),
                                available=to_string(len(compustate.stack)))

        if len(compustate.stack) - in_args + out_args > 1024:
            return vm_exception('STACK SIZE LIMIT EXCEEDED',
                                op=op,
                                pre_height=to_string(len(compustate.stack)))

        # Apply operation
        compustate.gas -= fee
        compustate.pc += 1

        if trace_vm:
            """
            This diverges from normal logging, as we use the logging namespace
            only to decide which features get logged in 'eth.vm.op'
            i.e. tracing can not be activated by activating a sub
            like 'eth.vm.op.stack'
            """
            trace_data = {}
            trace_data['stack'] = list(map(to_string, list(compustate.stack)))
            if _prevop in ('MLOAD', 'MSTORE', 'MSTORE8', 'SHA3', 'CALL',
                           'CALLCODE', 'CREATE', 'CALLDATACOPY', 'CODECOPY',
                           'EXTCODECOPY'):
                if len(compustate.memory) < 1024:
                    trace_data['memory'] = \
                        b''.join([encode_hex(ascii_chr(x)) for x
                                  in compustate.memory])
                else:
                    trace_data['sha3memory'] = \
                        encode_hex(utils.sha3(''.join([ascii_chr(x) for
                                              x in compustate.memory])))
            if _prevop in ('SSTORE', 'SLOAD') or steps == 0:
                trace_data['storage'] = ext.log_storage(msg.to)
            trace_data['gas'] = to_string(compustate.gas + fee)
            trace_data['inst'] = opcode
            trace_data['pc'] = to_string(compustate.pc - 1)
            if steps == 0:
                trace_data['depth'] = msg.depth
                trace_data['address'] = msg.to
            trace_data['op'] = op
            trace_data['steps'] = steps
            if op[:4] == 'PUSH':
                trace_data['pushvalue'] = pushval
            log_vm_op.trace('vm', **trace_data)
            steps += 1
            _prevop = op

        # Invalid operation
        if op == 'INVALID':
            return vm_exception('INVALID OP', opcode=opcode)

        # Valid operations
        if opcode < 0x10:
            if op == 'STOP':
                return peaceful_exit('STOP', compustate.gas, [])
            elif op == 'ADD':
                stk.append((stk.pop() + stk.pop()) & TT256M1)
            elif op == 'SUB':
                stk.append((stk.pop() - stk.pop()) & TT256M1)
            elif op == 'MUL':
                stk.append((stk.pop() * stk.pop()) & TT256M1)
            elif op == 'DIV':
                s0, s1 = stk.pop(), stk.pop()
                stk.append(0 if s1 == 0 else s0 // s1)
            elif op == 'MOD':
                s0, s1 = stk.pop(), stk.pop()
                stk.append(0 if s1 == 0 else s0 % s1)
            elif op == 'SDIV':
                s0, s1 = utils.to_signed(stk.pop()), utils.to_signed(stk.pop())
                stk.append(0 if s1 == 0 else (abs(s0) // abs(s1) *
                                              (-1 if s0 * s1 < 0 else 1)) & TT256M1)
            elif op == 'SMOD':
                s0, s1 = utils.to_signed(stk.pop()), utils.to_signed(stk.pop())
                stk.append(0 if s1 == 0 else (abs(s0) % abs(s1) *
                                              (-1 if s0 < 0 else 1)) & TT256M1)
            elif op == 'ADDMOD':
                s0, s1, s2 = stk.pop(), stk.pop(), stk.pop()
                stk.append((s0 + s1) % s2 if s2 else 0)
            elif op == 'MULMOD':
                s0, s1, s2 = stk.pop(), stk.pop(), stk.pop()
                stk.append((s0 * s1) % s2 if s2 else 0)
            elif op == 'EXP':
                base, exponent = stk.pop(), stk.pop()
                # fee for exponent is dependent on its bytes
                # calc n bytes to represent exponent
                nbytes = len(utils.encode_int(exponent))
                expfee = nbytes * opcodes.GEXPONENTBYTE
                if compustate.gas < expfee:
                    compustate.gas = 0
                    return vm_exception('OOG EXPONENT')
                compustate.gas -= expfee
                stk.append(pow(base, exponent, TT256))
            elif op == 'SIGNEXTEND':
                s0, s1 = stk.pop(), stk.pop()
                if s0 <= 31:
                    testbit = s0 * 8 + 7
                    if s1 & (1 << testbit):
                        stk.append(s1 | (TT256 - (1 << testbit)))
                    else:
                        stk.append(s1 & ((1 << testbit) - 1))
                else:
                    stk.append(s1)
        elif opcode < 0x20:
            if op == 'LT':
                stk.append(1 if stk.pop() < stk.pop() else 0)
            elif op == 'GT':
                stk.append(1 if stk.pop() > stk.pop() else 0)
            elif op == 'SLT':
                s0, s1 = utils.to_signed(stk.pop()), utils.to_signed(stk.pop())
                stk.append(1 if s0 < s1 else 0)
            elif op == 'SGT':
                s0, s1 = utils.to_signed(stk.pop()), utils.to_signed(stk.pop())
                stk.append(1 if s0 > s1 else 0)
            elif op == 'EQ':
                stk.append(1 if stk.pop() == stk.pop() else 0)
            elif op == 'ISZERO':
                stk.append(0 if stk.pop() else 1)
            elif op == 'AND':
                stk.append(stk.pop() & stk.pop())
            elif op == 'OR':
                stk.append(stk.pop() | stk.pop())
            elif op == 'XOR':
                stk.append(stk.pop() ^ stk.pop())
            elif op == 'NOT':
                stk.append(TT256M1 - stk.pop())
            elif op == 'BYTE':
                s0, s1 = stk.pop(), stk.pop()
                if s0 >= 32:
                    stk.append(0)
                else:
                    stk.append((s1 // 256 ** (31 - s0)) % 256)
        elif opcode < 0x40:
            if op == 'SHA3':
                s0, s1 = stk.pop(), stk.pop()
                compustate.gas -= opcodes.GSHA3WORD * (utils.ceil32(s1) // 32)
                if compustate.gas < 0:
                    return vm_exception('OOG PAYING FOR SHA3')
                if not mem_extend(mem, compustate, op, s0, s1):
                    return vm_exception('OOG EXTENDING MEMORY')
                data = b''.join(map(ascii_chr, mem[s0: s0 + s1]))
                stk.append(utils.big_endian_to_int(utils.sha3(data)))
            elif op == 'ADDRESS':
                stk.append(utils.coerce_to_int(msg.to))
            elif op == 'BALANCE':
                # EIP150: Increase the gas cost of BALANCE to 400
                if ext.post_anti_dos_hardfork:
                    if not eat_gas(compustate, opcodes.BALANCE_SUPPLEMENTAL_GAS):
                        return vm_exception("OUT OF GAS")
                addr = utils.coerce_addr_to_hex(stk.pop() % 2 ** 160)
                stk.append(ext.get_balance(addr))
            elif op == 'ORIGIN':
                stk.append(utils.coerce_to_int(ext.tx_origin))
            elif op == 'CALLER':
                stk.append(utils.coerce_to_int(msg.sender))
            elif op == 'CALLVALUE':
                stk.append(msg.value)
            elif op == 'CALLDATALOAD':
                stk.append(msg.data.extract32(stk.pop()))
            elif op == 'CALLDATASIZE':
                stk.append(msg.data.size)
            elif op == 'CALLDATACOPY':
                mstart, dstart, size = stk.pop(), stk.pop(), stk.pop()
                if not mem_extend(mem, compustate, op, mstart, size):
                    return vm_exception('OOG EXTENDING MEMORY')
                if not data_copy(compustate, size):
                    return vm_exception('OOG COPY DATA')
                msg.data.extract_copy(mem, mstart, dstart, size)
            elif op == 'CODESIZE':
                stk.append(len(processed_code))
            elif op == 'CODECOPY':
                start, s1, size = stk.pop(), stk.pop(), stk.pop()
                if not mem_extend(mem, compustate, op, start, size):
                    return vm_exception('OOG EXTENDING MEMORY')
                if not data_copy(compustate, size):
                    return vm_exception('OOG COPY DATA')
                for i in range(size):
                    if s1 + i < len(processed_code):
                        mem[start + i] = processed_code[s1 + i][4]
                    else:
                        mem[start + i] = 0
            elif op == 'GASPRICE':
                stk.append(ext.tx_gasprice)
            elif op == 'EXTCODESIZE':
                # EIP150: Increase the gas cost of EXTCODESIZE to 700
                if ext.post_anti_dos_hardfork:
                    if not eat_gas(compustate, opcodes.EXTCODELOAD_SUPPLEMENTAL_GAS):
                        return vm_exception("OUT OF GAS")
                addr = utils.coerce_addr_to_hex(stk.pop() % 2 ** 160)
                stk.append(len(ext.get_code(addr) or b''))
            elif op == 'EXTCODECOPY':
                # EIP150: Increase the base gas cost of EXTCODECOPY to 700
                if ext.post_anti_dos_hardfork:
                    if not eat_gas(compustate, opcodes.EXTCODELOAD_SUPPLEMENTAL_GAS):
                        return vm_exception("OUT OF GAS")
                addr = utils.coerce_addr_to_hex(stk.pop() % 2 ** 160)
                start, s2, size = stk.pop(), stk.pop(), stk.pop()
                extcode = ext.get_code(addr) or b''
                assert utils.is_string(extcode)
                if not mem_extend(mem, compustate, op, start, size):
                    return vm_exception('OOG EXTENDING MEMORY')
                if not data_copy(compustate, size):
                    return vm_exception('OOG COPY DATA')
                for i in range(size):
                    if s2 + i < len(extcode):
                        mem[start + i] = utils.safe_ord(extcode[s2 + i])
                    else:
                        mem[start + i] = 0
        elif opcode < 0x50:
            if op == 'BLOCKHASH':
                stk.append(utils.big_endian_to_int(ext.block_hash(stk.pop())))
            elif op == 'COINBASE':
                stk.append(utils.big_endian_to_int(ext.block_coinbase))
            elif op == 'TIMESTAMP':
                stk.append(ext.block_timestamp)
            elif op == 'NUMBER':
                stk.append(ext.block_number)
            elif op == 'DIFFICULTY':
                stk.append(ext.block_difficulty)
            elif op == 'GASLIMIT':
                stk.append(ext.block_gas_limit)
        elif opcode < 0x60:
            if op == 'POP':
                stk.pop()
            elif op == 'MLOAD':
                s0 = stk.pop()
                if not mem_extend(mem, compustate, op, s0, 32):
                    return vm_exception('OOG EXTENDING MEMORY')
                data = b''.join(map(ascii_chr, mem[s0: s0 + 32]))
                stk.append(utils.big_endian_to_int(data))
            elif op == 'MSTORE':
                s0, s1 = stk.pop(), stk.pop()
                if not mem_extend(mem, compustate, op, s0, 32):
                    return vm_exception('OOG EXTENDING MEMORY')
                v = s1
                for i in range(31, -1, -1):
                    mem[s0 + i] = v % 256
                    v //= 256
            elif op == 'MSTORE8':
                s0, s1 = stk.pop(), stk.pop()
                if not mem_extend(mem, compustate, op, s0, 1):
                    return vm_exception('OOG EXTENDING MEMORY')
                mem[s0] = s1 % 256
            elif op == 'SLOAD':
                # EIP150: Increase the gas cost of SLOAD to 200
                if ext.post_anti_dos_hardfork:
                    if not eat_gas(compustate, opcodes.SLOAD_SUPPLEMENTAL_GAS):
                        return vm_exception("OUT OF GAS")
                stk.append(ext.get_storage_data(msg.to, stk.pop()))
            elif op == 'SSTORE':
                s0, s1 = stk.pop(), stk.pop()
                if ext.get_storage_data(msg.to, s0):
                    gascost = opcodes.GSTORAGEMOD if s1 else opcodes.GSTORAGEKILL
                    refund = 0 if s1 else opcodes.GSTORAGEREFUND
                else:
                    gascost = opcodes.GSTORAGEADD if s1 else opcodes.GSTORAGEMOD
                    refund = 0
                if compustate.gas < gascost:
                    return vm_exception('OUT OF GAS')
                compustate.gas -= gascost
                ext.add_refund(refund)  # adds neg gascost as a refund if below zero
                ext.set_storage_data(msg.to, s0, s1)
            elif op == 'JUMP':
                compustate.pc = stk.pop()
                opnew = processed_code[compustate.pc][0] if \
                    compustate.pc < len(processed_code) else 'STOP'
                if opnew != 'JUMPDEST':
                    return vm_exception('BAD JUMPDEST')
            elif op == 'JUMPI':
                s0, s1 = stk.pop(), stk.pop()
                if s1:
                    compustate.pc = s0
                    opnew = processed_code[compustate.pc][0] if \
                        compustate.pc < len(processed_code) else 'STOP'
                    if opnew != 'JUMPDEST':
                        return vm_exception('BAD JUMPDEST')
            elif op == 'PC':
                stk.append(compustate.pc - 1)
            elif op == 'MSIZE':
                stk.append(len(mem))
            elif op == 'GAS':
                stk.append(compustate.gas)  # AFTER subtracting cost 1
        elif op[:4] == 'PUSH':
            pushnum = int(op[4:])
            compustate.pc += pushnum
            stk.append(pushval)
        elif op[:3] == 'DUP':
            depth = int(op[3:])
            stk.append(stk[-depth])
        elif op[:4] == 'SWAP':
            depth = int(op[4:])
            temp = stk[-depth - 1]
            stk[-depth - 1] = stk[-1]
            stk[-1] = temp

        elif op[:3] == 'LOG':
            """
            0xa0 ... 0xa4, 32/64/96/128/160 + len(data) gas
            a. Opcodes LOG0...LOG4 are added, takes 2-6 stack arguments
                    MEMSTART MEMSZ (TOPIC1) (TOPIC2) (TOPIC3) (TOPIC4)
            b. Logs are kept track of during tx execution exactly the same way as suicides
               (except as an ordered list, not a set).
               Each log is in the form [address, [topic1, ... ], data] where:
               * address is what the ADDRESS opcode would output
               * data is mem[MEMSTART: MEMSTART + MEMSZ]
               * topics are as provided by the opcode
            c. The ordered list of logs in the transaction are expressed as [log0, log1, ..., logN].
            """
            depth = int(op[3:])
            mstart, msz = stk.pop(), stk.pop()
            topics = [stk.pop() for x in range(depth)]
            compustate.gas -= msz * opcodes.GLOGBYTE
            if not mem_extend(mem, compustate, op, mstart, msz):
                return vm_exception('OOG EXTENDING MEMORY')
            data = b''.join(map(ascii_chr, mem[mstart: mstart + msz]))
            ext.log(msg.to, topics, data)
            log_log.trace('LOG', to=msg.to, topics=topics, data=list(map(utils.safe_ord, data)))
            # print('LOG', msg.to, topics, list(map(ord, data)))

        elif op == 'CREATE':
            value, mstart, msz = stk.pop(), stk.pop(), stk.pop()
            if not mem_extend(mem, compustate, op, mstart, msz):
                return vm_exception('OOG EXTENDING MEMORY')
            if ext.get_balance(msg.to) >= value and msg.depth < 1024:
                cd = CallData(mem, mstart, msz)
                ingas = compustate.gas
                # EIP150(1b) CREATE only provides all but one 64th of the
                # parent gas to the child call
                if ext.post_anti_dos_hardfork:
                    ingas = max_call_gas(ingas)

                create_msg = Message(msg.to, b'', value, ingas, cd, msg.depth + 1)
                o, gas, addr = ext.create(create_msg)
                if o:
                    stk.append(utils.coerce_to_int(addr))
                    compustate.gas -= (ingas - gas)
                else:
                    stk.append(0)
                    compustate.gas -= ingas
            else:
                stk.append(0)
        elif op == 'CALL':
            gas, to, value, meminstart, meminsz, memoutstart, memoutsz = \
                stk.pop(), stk.pop(), stk.pop(), stk.pop(), stk.pop(), stk.pop(), stk.pop()
            if not mem_extend(mem, compustate, op, meminstart, meminsz) or \
                    not mem_extend(mem, compustate, op, memoutstart, memoutsz):
                return vm_exception('OOG EXTENDING MEMORY')
            to = utils.encode_int(to)
            to = ((b'\x00' * (32 - len(to))) + to)[12:]
            extra_gas = (not ext.account_exists(to)) * opcodes.GCALLNEWACCOUNT + \
                    (value > 0) * opcodes.GCALLVALUETRANSFER + \
                    ext.post_anti_dos_hardfork * opcodes.CALL_SUPPLEMENTAL_GAS
                    # ^ EIP150 Increase the gas cost of CALL to 700

            if ext.post_anti_dos_hardfork:
                # EIP150(1b) if a call asks for more gas than all but one 64th of
                # the maximum allowed amount, call with all but one 64th of the
                # maximum allowed amount of gas
                if compustate.gas < extra_gas:
                    return vm_exception('OUT OF GAS', needed=extra_gas)
                gas = min(gas, max_call_gas(compustate.gas - extra_gas))
            else:
                if compustate.gas < gas + extra_gas:
                    return vm_exception('OUT OF GAS', needed=gas + extra_gas)

            submsg_gas = gas + opcodes.GSTIPEND * (value > 0)
            if ext.get_balance(msg.to) >= value and msg.depth < 1024:
                compustate.gas -= (gas + extra_gas)
                cd = CallData(mem, meminstart, meminsz)
                call_msg = Message(msg.to, to, value, submsg_gas, cd,
                                   msg.depth + 1, code_address=to)
                result, gas, data = ext.msg(call_msg)
                if result == 0:
                    stk.append(0)
                else:
                    stk.append(1)
                    compustate.gas += gas
                    for i in range(min(len(data), memoutsz)):
                        mem[memoutstart + i] = data[i]
            else:
                compustate.gas -= (gas + extra_gas - submsg_gas)
                stk.append(0)
        elif op == 'CALLCODE' or op == 'DELEGATECALL':
            if op == 'CALLCODE':
                gas, to, value, meminstart, meminsz, memoutstart, memoutsz = \
                    stk.pop(), stk.pop(), stk.pop(), stk.pop(), stk.pop(), stk.pop(), stk.pop()
            else:
                gas, to, meminstart, meminsz, memoutstart, memoutsz = \
                    stk.pop(), stk.pop(), stk.pop(), stk.pop(), stk.pop(), stk.pop()
                value = 0
            if not mem_extend(mem, compustate, op, meminstart, meminsz) or \
                    not mem_extend(mem, compustate, op, memoutstart, memoutsz):
                return vm_exception('OOG EXTENDING MEMORY')
            extra_gas = (value > 0) * opcodes.GCALLVALUETRANSFER + \
                    ext.post_anti_dos_hardfork * opcodes.CALL_SUPPLEMENTAL_GAS
                    # ^ EIP150 Increase the gas cost of CALLCODE, DELEGATECALL to 700

            if ext.post_anti_dos_hardfork:
                # EIP150(1b) if a call asks for more gas than all but one 64th of
                # the maximum allowed amount, call with all but one 64th of the
                # maximum allowed amount of gas
                if compustate.gas < extra_gas:
                    return vm_exception('OUT OF GAS', needed=extra_gas)
                gas = min(gas, max_call_gas(compustate.gas - extra_gas))
            else:
                if compustate.gas < gas + extra_gas:
                    return vm_exception('OUT OF GAS', needed=gas + extra_gas)

            submsg_gas = gas + opcodes.GSTIPEND * (value > 0)
            if ext.get_balance(msg.to) >= value and msg.depth < 1024:
                compustate.gas -= (gas + extra_gas)
                to = utils.encode_int(to)
                to = ((b'\x00' * (32 - len(to))) + to)[12:]
                cd = CallData(mem, meminstart, meminsz)
                if ext.post_homestead_hardfork and op == 'DELEGATECALL':
                    call_msg = Message(msg.sender, msg.to, msg.value, submsg_gas, cd,
                                       msg.depth + 1, code_address=to, transfers_value=False)
                elif op == 'DELEGATECALL':
                    return vm_exception('OPCODE INACTIVE')
                else:
                    call_msg = Message(msg.to, msg.to, value, submsg_gas, cd,
                                       msg.depth + 1, code_address=to)
                result, gas, data = ext.msg(call_msg)
                if result == 0:
                    stk.append(0)
                else:
                    stk.append(1)
                    compustate.gas += gas
                    for i in range(min(len(data), memoutsz)):
                        mem[memoutstart + i] = data[i]
            else:
                compustate.gas -= (gas + extra_gas - submsg_gas)
                stk.append(0)
        elif op == 'RETURN':
            s0, s1 = stk.pop(), stk.pop()
            if not mem_extend(mem, compustate, op, s0, s1):
                return vm_exception('OOG EXTENDING MEMORY')
            return peaceful_exit('RETURN', compustate.gas, mem[s0: s0 + s1])
        elif op == 'SUICIDE':
            to = utils.encode_int(stk.pop())
            to = ((b'\x00' * (32 - len(to))) + to)[12:]

            if ext.post_anti_dos_hardfork:
                # EIP150 Increase the gas cost of SUICIDE to 5000
                extra_gas = opcodes.SUICIDE_SUPPLEMENTAL_GAS + \
                        (not ext.account_exists(to)) * opcodes.GCALLNEWACCOUNT
                # ^ EIP150(1c) If SUICIDE hits a newly created account, it
                # triggers an additional gas cost of 25000 (similar to CALLs)
                if not eat_gas(compustate, extra_gas):
                    return vm_exception("OUT OF GAS")

            xfer = ext.get_balance(msg.to)
            ext.set_balance(to, ext.get_balance(to) + xfer)
            ext.set_balance(msg.to, 0)
            ext.add_suicide(msg.to)
            # print('suiciding %s %s %d' % (msg.to, to, xfer))
            return 1, compustate.gas, []

        # this is slow!
        # for a in stk:
        #     assert is_numeric(a), (op, stk)
        #     assert a >= 0 and a < 2**256, (a, op, stk)