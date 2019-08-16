# coding=utf-8
# Copyright 2017-2019 The THUMT Authors

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import logging
import os
import six
import time
import torch
import torch.distributed as distributed
from torch.multiprocessing import Process

import thumt.data as data
import thumt.utils as utils
import thumt.losses as losses
import thumt.models as models
import thumt.optimizers as optimizers


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Training neural machine translation models",
        usage="trainer.py [<args>] [-h | --help]"
    )

    # input files
    parser.add_argument("--input", type=str, nargs=2,
                        help="Path of source and target corpus")
    parser.add_argument("--record", type=str,
                        help="Path to tf.Record data")
    parser.add_argument("--output", type=str, default="train",
                        help="Path to saved models")
    parser.add_argument("--vocabulary", type=str, nargs=2,
                        help="Path of source and target vocabulary")
    parser.add_argument("--validation", type=str,
                        help="Path of validation file")
    parser.add_argument("--references", type=str, nargs="+",
                        help="Path of reference files")
    parser.add_argument("--checkpoint", type=str,
                        help="Path to pre-trained checkpoint")

    # model and configuration
    parser.add_argument("--model", type=str, required=True,
                        help="Name of the model")
    parser.add_argument("--parameters", type=str, default="",
                        help="Additional hyper parameters")

    return parser.parse_args(args)


def default_params():
    params = utils.HParams(
        input=["", ""],
        output="",
        record="",
        model="transformer",
        vocab=["", ""],
        pad="<pad>",
        bos="<eos>",
        eos="<eos>",
        unk="<unk>",
        # Dataset
        epochs=5,
        batch_size=4096,
        batch_multiplier=1,
        fixed_batch_size=False,
        min_length=1,
        max_length=256,
        buffer_size=10000,
        # Training
        warmup_steps=4000,
        train_steps=100000,
        device_list=[0],
        update_cycle=1,
        initializer="uniform_unit_scaling",
        initializer_gain=1.0,
        scale_l1=0.0,
        scale_l2=0.0,
        optimizer="Adam",
        adam_beta1=0.9,
        adam_beta2=0.999,
        adam_epsilon=1e-8,
        clip_grad_norm=5.0,
        learning_rate=1.0,
        learning_rate_schedule="linear_warmup_rsqrt_decay",
        learning_rate_boundaries=[0],
        learning_rate_values=[0.0],
        keep_checkpoint_max=20,
        keep_top_checkpoint_max=5,
        save_checkpoint_secs=0,
        save_checkpoint_steps=1000,
        # Validation
        eval_steps=2000,
        eval_secs=0,
        eval_batch_size=32,
        top_beams=1,
        beam_size=4,
        decode_alpha=0.6,
        decode_length=50,
        validation="",
        references=[""],
    )

    return params


def import_params(model_dir, model_name, params):
    model_dir = os.path.abspath(model_dir)
    p_name = os.path.join(model_dir, "params.json")
    m_name = os.path.join(model_dir, model_name + ".json")

    if not os.path.exists(p_name) or not os.path.exists(m_name):
        return params

    with open(p_name) as fd:
        logging.info("Restoring hyper parameters from %s" % p_name)
        json_str = fd.readline()
        params.parse_json(json_str)

    with open(m_name) as fd:
        logging.info("Restoring model parameters from %s" % m_name)
        json_str = fd.readline()
        params.parse_json(json_str)

    return params


def export_params(output_dir, name, params):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Save params as params.json
    filename = os.path.join(output_dir, name)

    with open(filename, "w") as fd:
        fd.write(params.to_json())


def merge_params(params1, params2):
    params = utils.HParams()

    for (k, v) in six.iteritems(params1.values()):
        params.add_hparam(k, v)

    params_dict = params.values()

    for (k, v) in six.iteritems(params2.values()):
        if k in params_dict:
            # Override
            setattr(params, k, v)
        else:
            params.add_hparam(k, v)

    return params


def override_params(params, args):
    params.model = args.model or params.model
    params.input = args.input or params.input
    params.output = args.output or params.output
    params.record = args.record or params.record
    params.vocab = args.vocabulary or params.vocab
    params.validation = args.validation or params.validation
    params.references = args.references or params.references
    params.parse(args.parameters)

    src_vocab, src_w2idx, src_idx2w = data.load_vocabulary(params.vocab[0])
    tgt_vocab, tgt_w2idx, tgt_idx2w = data.load_vocabulary(params.vocab[1])

    params.vocabulary = {
        "source": src_vocab, "target": tgt_vocab
    }
    params.lookup = {
        "source": src_w2idx, "target": tgt_w2idx
    }
    params.mapping = {
        "source": src_idx2w, "target": tgt_idx2w
    }

    return params


def collect_params(all_params, params):
    collected = utils.HParams()

    for k in six.iterkeys(params.values()):
        collected.add_hparam(k, getattr(all_params, k))

    return collected


def print_variables(model):
    weights = {v[0]: v[1] for v in model.named_parameters()}
    total_size = 0

    for name in sorted(list(weights)):
        v = weights[name]
        print("%s %s" % (name.ljust(60), str(list(v.shape)).rjust(15)))
        total_size += v.nelement()

    print("Total trainable variables size: %d" % total_size)


def main(params):
    # Set device
    torch.cuda.set_device(params.device_list[distributed.get_rank()])
    model_cls = models.get_model(params.model)
    model = model_cls(params).cuda()

    # Export parameters
    if distributed.get_rank() == 0:
        export_params(params.output, "params.json", params)
        export_params(params.output, "%s.json" % params.model,
                      collect_params(params, model_cls.default_params()))

    if distributed.get_rank() == 0:
        print_variables(model)

    schedule = optimizers.LinearWarmupRsqrtDecay(params.learning_rate,
                                                 params.warmup_steps)
    optimizer = optimizers.AdamOptimizer(learning_rate=schedule,
                                         beta_1=params.adam_beta1,
                                         beta_2=params.adam_beta2,
                                         epsilon=params.adam_epsilon)

    step = 0
    dataset = data.get_dataset(params.input, "train", params)

    def train_fn(features):
        labels = features["labels"]
        mask = torch.ne(labels, 0).to(torch.float32)
        logits = model(features)
        loss = losses.smoothed_softmax_cross_entropy_with_logits(
            logits=logits, labels=labels, smoothing=params.label_smoothing)
        return torch.mean(loss * mask)

    for i in range(params.epochs):
        for features in dataset:
            step += 1
            t = time.time()
            features = data.lookup(features, "train", params)
            loss = train_fn(features)
            gradients = optimizer.compute_gradients(loss,
                                                    model.parameters())
            optimizer.apply_gradients(zip(gradients,
                                          list(model.parameters())))

            t = time.time() - t
            print("epoch = %d, step = %d, loss = %.3f (%.3f sec)" %
                  (i + 1, step, float(loss), t))

            if step % params.save_checkpoint_steps == 0:
                if distributed.get_rank() == 0:
                    name = "/model-%d.pt" % step
                    torch.save(model.state_dict(), params.output + name)

        if distributed.get_rank() == 0:
            name = params.output + "/model-iter-%d.pt" % (i + 1)
            torch.save(model.state_dict(), name)


def init_processes(rank, size, params, fn):
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = "29500"
    distributed.init_process_group("nccl", rank=rank, world_size=size)
    fn(params)


if __name__ == "__main__":
    args = parse_args()
    model_cls = models.get_model(args.model)

    # Import and override parameters
    # Priorities (low -> high):
    # default -> saved -> command
    params = default_params()
    params = merge_params(params, model_cls.default_params())
    params = import_params(args.output, args.model, params)
    params = override_params(params, args)

    size = len(params.device_list)

    torch.multiprocessing.spawn(init_processes,
                                args=(size, params, main),
                                nprocs=size)
