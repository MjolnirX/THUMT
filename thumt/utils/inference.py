# coding=utf-8
# Copyright 2017-2019 The THUMT Authors

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import torch
import tensorflow as tf

from collections import namedtuple


def _merge_first_two_dims(tensor):
    shape = list(tensor.shape)
    shape[1] *= shape[0]
    return torch.reshape(tensor, shape[1:])


def _split_first_two_dims(tensor, dim_0, dim_1):
    shape = [dim_0, dim_1] + list(tensor.shape)[1:]
    return torch.reshape(tensor, shape)


def _tile_to_beam_size(tensor, beam_size):
    tensor = torch.unsqueeze(tensor, 1)
    tile_dims = [1] * int(tensor.dim())
    tile_dims[1] = beam_size

    return tensor.repeat(tile_dims)


def _gather_2d(params, indices, name=None):
    batch_size = params.shape[0]
    range_size = indices.shape[1]
    batch_pos = torch.arange(batch_size * range_size, device=params.device)
    batch_pos = batch_pos // range_size
    batch_pos = torch.reshape(batch_pos, [batch_size, range_size])
    output = params[batch_pos, indices]

    return output


class BeamSearchState(namedtuple("BeamSearchState",
                                 ("inputs", "state", "finish"))):
    pass


def _get_inference_fn(model_fns, features):
    def inference_fn(inputs, state):
        local_features = {
            "source": features["source"],
            "target": inputs,
        }

        outputs = []
        next_state = []

        for (model_fn, model_state) in zip(model_fns, state):
            if model_state:
                logits, new_state = model_fn(local_features, model_state)
                outputs.append(torch.nn.functional.log_softmax(logits,
                                                               dim=-1))
                next_state.append(new_state)
            else:
                logits = model_fn(local_features)
                outputs.append(torch.nn.functional.log_softmax(logits,
                                                               dim=-1))
                next_state.append({})

        # Ensemble
        log_prob = sum(outputs) / float(len(outputs))

        return log_prob.float(), next_state

    return inference_fn


def _beam_search_step(time, func, state, batch_size, beam_size, alpha,
                      pad_id, eos_id):
    # Compute log probabilities
    seqs, log_probs = state.inputs[:2]
    flat_seqs = _merge_first_two_dims(seqs)
    flat_state = tf.nest.map_structure(lambda x: _merge_first_two_dims(x),
                                       state.state)
    step_log_probs, next_state = func(flat_seqs, flat_state)
    step_log_probs = _split_first_two_dims(step_log_probs, batch_size,
                                           beam_size)
    next_state = tf.nest.map_structure(
        lambda x: _split_first_two_dims(x, batch_size, beam_size), next_state)
    curr_log_probs = torch.unsqueeze(log_probs, 2) + step_log_probs

    # Apply length penalty
    # time or time + 1 ?
    length_penalty = ((5.0 + float(time + 1)) / 6.0) ** alpha
    curr_scores = curr_log_probs / length_penalty
    vocab_size = curr_scores.shape[-1]

    # Select top-k candidates
    # [batch_size, beam_size * vocab_size]
    curr_scores = torch.reshape(curr_scores, [-1, beam_size * vocab_size])
    # [batch_size, 2 * beam_size]
    top_scores, top_indices = torch.topk(curr_scores, k=2*beam_size)
    # Shape: [batch_size, 2 * beam_size]
    beam_indices = top_indices // vocab_size
    symbol_indices = top_indices % vocab_size
    # Expand sequences
    # [batch_size, 2 * beam_size, time]
    candidate_seqs = _gather_2d(seqs, beam_indices)
    candidate_seqs = torch.cat([candidate_seqs,
                                torch.unsqueeze(symbol_indices, 2)], 2)

    # Expand sequences
    # Suppress finished sequences
    flags = torch.eq(symbol_indices, eos_id).to(torch.bool)
    # [batch, 2 * beam_size]
    alive_scores = top_scores + flags.to(torch.float32) * -1e9
    # [batch, beam_size]
    alive_scores, alive_indices = torch.topk(alive_scores, beam_size)
    alive_symbols = _gather_2d(symbol_indices, alive_indices)
    alive_indices = _gather_2d(beam_indices, alive_indices)
    alive_seqs = _gather_2d(seqs, alive_indices)
    # [batch_size, beam_size, time + 1]
    alive_seqs = torch.cat([alive_seqs, torch.unsqueeze(alive_symbols, 2)], 2)
    alive_state = tf.nest.map_structure(
        lambda x: _gather_2d(x, alive_indices),
        next_state)
    alive_log_probs = alive_scores * length_penalty

    # Select finished sequences
    prev_fin_flags, prev_fin_seqs, prev_fin_scores = state.finish
    # [batch, 2 * beam_size]
    step_fin_scores = top_scores + (1.0 - flags.to(torch.float32)) * -1e9
    # [batch, 3 * beam_size]
    fin_flags = torch.cat([prev_fin_flags, flags], dim=1)
    fin_scores = torch.cat([prev_fin_scores, step_fin_scores], dim=1)
    # [batch, beam_size]
    fin_scores, fin_indices = torch.topk(fin_scores, beam_size)
    fin_flags = _gather_2d(fin_flags, fin_indices)
    pad_seqs = prev_fin_seqs.new_full([batch_size, beam_size, 1], pad_id)
    prev_fin_seqs = torch.cat([prev_fin_seqs, pad_seqs], dim=2)
    fin_seqs = torch.cat([prev_fin_seqs, candidate_seqs], dim=1)
    fin_seqs = _gather_2d(fin_seqs, fin_indices)

    new_state = BeamSearchState(
        inputs=(alive_seqs, alive_log_probs, alive_scores),
        state=alive_state,
        finish=(fin_flags, fin_seqs, fin_scores),
    )

    return new_state


def beam_search(models, features, params):
    if not isinstance(models, (list, tuple)):
        raise ValueError("'models' must be a list or tuple")

    beam_size = params.beam_size
    top_beams = params.top_beams
    alpha = params.decode_alpha
    decode_length = params.decode_length

    pad_id = params.lookup["target"][params.pad.encode("utf-8")]
    bos_id = params.lookup["target"][params.bos.encode("utf-8")]
    eos_id = params.lookup["target"][params.eos.encode("utf-8")]

    min_val = -1e9
    shape = features["source"].shape
    device = features["source"].device
    batch_size = shape[0]
    seq_length = shape[1]

    # Compute initial state if necessary
    states = []
    funcs = []

    for model in models:
        state = model.empty_state(batch_size, device)
        states.append(model.encode(features, state))
        funcs.append(model.decode)

    # Expand the inputs
    # [batch, length] => [batch * beam_size, length]
    features["source"] = torch.unsqueeze(features["source"], 1)
    features["source"] = features["source"].repeat([1, beam_size, 1])
    features["source"] = torch.reshape(features["source"],
                                       [batch_size * beam_size, seq_length])

    # For source sequence length
    max_step = seq_length + decode_length

    decoding_fn = _get_inference_fn(funcs, features)

    states = tf.nest.map_structure(
        lambda x: _tile_to_beam_size(x, beam_size),
        states)

    # Initial beam search state
    init_seqs = torch.full([batch_size, beam_size, 1], bos_id, device=device)
    init_seqs = init_seqs.long()
    init_log_probs = init_seqs.new_tensor(
        [[0.] + [min_val] * (beam_size - 1)], dtype=torch.float32)
    init_log_probs = init_log_probs.repeat([batch_size, 1])
    init_scores = torch.zeros_like(init_log_probs)
    fin_seqs = torch.zeros([batch_size, beam_size, 1], dtype=torch.int64,
                           device=device)
    fin_scores = torch.full([batch_size, beam_size], min_val,
                            dtype=torch.float32, device=device)
    fin_flags = torch.zeros([batch_size, beam_size], dtype=torch.bool,
                            device=device)

    state = BeamSearchState(
        inputs=(init_seqs, init_log_probs, init_scores),
        state=states,
        finish=(fin_flags, fin_seqs, fin_scores),
    )

    for time in range(max_step):
        state = _beam_search_step(time, decoding_fn, state, batch_size,
                                  beam_size, alpha, pad_id, eos_id)
        max_penalty = ((5.0 + max_step) / 6.0) ** alpha
        best_alive_score = torch.max(state.inputs[1][:, 0] / max_penalty)
        worst_finished_score = torch.min(state.finish[2])
        cond = torch.gt(worst_finished_score, best_alive_score)
        is_finished = bool(cond)

        if is_finished:
            break

    final_state = state
    alive_seqs = final_state.inputs[0]
    alive_scores = final_state.inputs[2]
    final_flags = final_state.finish[0].byte()
    final_seqs = final_state.finish[1]
    final_scores = final_state.finish[2]

    final_seqs = torch.where(final_flags.any(1)[:, None, None], final_seqs,
                             alive_seqs)
    final_scores = torch.where(final_flags.any(1)[:, None, None], final_scores,
                            alive_scores)

    return final_seqs[:, :top_beams, 1:], final_scores[:, :top_beams]
