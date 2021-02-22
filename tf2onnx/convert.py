# SPDX-License-Identifier: Apache-2.0


"""
python -m tf2onnx.convert : tool to convert a frozen tensorflow graph to onnx
"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

# pylint: disable=unused-argument,unused-import,ungrouped-imports,wrong-import-position

import argparse
import os
import sys

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

import tensorflow as tf

from tf2onnx.tfonnx import process_tf_graph
from tf2onnx import constants, logging, utils, optimizer
from tf2onnx import tf_loader
from tf2onnx.graph import ExternalTensorStorage
from tf2onnx.tf_utils import compress_graph_def

# pylint: disable=unused-argument

_HELP_TEXT = """
Usage Examples:

python -m tf2onnx.convert --saved-model saved_model_dir --output model.onnx
python -m tf2onnx.convert --input frozen_graph.pb  --inputs X:0 --outputs output:0 --output model.onnx
python -m tf2onnx.convert --checkpoint checkpoint.meta  --inputs X:0 --outputs output:0 --output model.onnx

For help and additional information see:
    https://github.com/onnx/tensorflow-onnx

If you run into issues, open an issue here:
    https://github.com/onnx/tensorflow-onnx/issues
"""


def get_args():
    """Parse commandline."""
    parser = argparse.ArgumentParser(description="Convert tensorflow graphs to ONNX.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=_HELP_TEXT)
    parser.add_argument("--input", help="input from graphdef")
    parser.add_argument("--graphdef", help="input from graphdef")
    parser.add_argument("--saved-model", help="input from saved model")
    parser.add_argument("--tag", help="tag to use for saved_model")
    parser.add_argument("--signature_def", help="signature_def from saved_model to use")
    parser.add_argument("--concrete_function", type=int, default=None,
                        help="For TF2.x saved_model, index of func signature in __call__ (--signature_def is ignored)")
    parser.add_argument("--checkpoint", help="input from checkpoint")
    parser.add_argument("--keras", help="input from keras model")
    parser.add_argument("--tflite", help="input from tflite model")
    parser.add_argument("--large_model", help="use the large model format (for models > 2GB)", action="store_true")
    parser.add_argument("--output", help="output model file")
    parser.add_argument("--inputs", help="model input_names (optional for saved_model, keras, and tflite)")
    parser.add_argument("--outputs", help="model output_names (optional for saved_model, keras, and tflite)")
    parser.add_argument("--ignore_default", help="comma-separated list of names of PlaceholderWithDefault "
                                                 "ops to change into Placeholder ops")
    parser.add_argument("--use_default", help="comma-separated list of names of PlaceholderWithDefault ops to "
                                              "change into Identity ops using their default value")
    parser.add_argument("--rename-inputs", help="input names to use in final model (optional)")
    parser.add_argument("--rename-outputs", help="output names to use in final model (optional)")
    parser.add_argument("--opset", type=int, default=None, help="opset version to use for onnx domain")
    parser.add_argument("--dequantize", help="Remove quantization from model. Only supported for tflite currently.",
                        action="store_true")
    parser.add_argument("--custom-ops", help="comma-separated map of custom ops to domains in format OpName:domain")
    parser.add_argument("--extra_opset", default=None,
                        help="extra opset with format like domain:version, e.g. com.microsoft:1")
    parser.add_argument("--target", default=",".join(constants.DEFAULT_TARGET), choices=constants.POSSIBLE_TARGETS,
                        help="target platform")
    parser.add_argument("--continue_on_error", help="continue_on_error", action="store_true")
    parser.add_argument("--verbose", "-v", help="verbose output, option is additive", action="count")
    parser.add_argument("--debug", help="debug mode", action="store_true")
    parser.add_argument("--output_frozen_graph", help="output frozen tf graph to file")
    parser.add_argument("--fold_const", help="Deprecated. Constant folding is always enabled.",
                        action="store_true")
    # experimental
    parser.add_argument("--inputs-as-nchw", help="transpose inputs as from nhwc to nchw")
    args = parser.parse_args()

    args.shape_override = None
    if args.input:
        # for backward compativility
        args.graphdef = args.input
    if args.graphdef or args.checkpoint:
        if not args.inputs or not args.outputs:
            parser.error("graphdef and checkpoint models need to provide inputs and outputs")
    if not any([args.graphdef, args.checkpoint, args.saved_model, args.keras, args.tflite]):
        parser.print_help()
        sys.exit(1)
    if args.inputs:
        args.inputs, args.shape_override = utils.split_nodename_and_shape(args.inputs)
    if args.outputs:
        args.outputs = args.outputs.split(",")
    if args.ignore_default:
        args.ignore_default = args.ignore_default.split(",")
    if args.use_default:
        args.use_default = args.use_default.split(",")
    if args.rename_outputs:
        args.rename_outputs = args.rename_outputs.split(",")
    if args.rename_inputs:
        args.rename_inputs = args.rename_inputs.split(",")
    if args.inputs_as_nchw:
        args.inputs_as_nchw = args.inputs_as_nchw.split(",")
    if args.target:
        args.target = args.target.split(",")
    if args.signature_def:
        args.signature_def = [args.signature_def]
    if args.dequantize:
        if not args.tflite:
            parser.error("dequantize flag is currently only supported for tflite")
    if args.extra_opset:
        tokens = args.extra_opset.split(':')
        if len(tokens) != 2:
            parser.error("invalid extra_opset argument")
        args.extra_opset = [utils.make_opsetid(tokens[0], int(tokens[1]))]

    return args

def make_default_custom_op_handler(domain):
    def default_custom_op_handler(ctx, node, name, args):
        node.domain = domain
        return node
    return default_custom_op_handler

def main():
    args = get_args()
    logging.basicConfig(level=logging.get_verbosity_level(args.verbose))
    if args.debug:
        utils.set_debug_mode(True)

    logger = logging.getLogger(constants.TF2ONNX_PACKAGE_NAME)

    extra_opset = args.extra_opset or []
    tflite_path = None
    custom_ops = {}
    initialized_tables = None
    tensors_to_rename = {}
    if args.custom_ops:
        using_tf_opset = False
        for op in args.custom_ops.split(","):
            if ":" in op:
                op, domain = op.split(":")
            else:
                # default custom ops for tensorflow-onnx are in the "tf" namespace
                using_tf_opset = True
                domain = constants.TENSORFLOW_OPSET.domain
            custom_ops[op] = (make_default_custom_op_handler(domain), [])
        if using_tf_opset:
            extra_opset.append(constants.TENSORFLOW_OPSET)

    if any(opset.domain == constants.CONTRIB_OPS_DOMAIN for opset in extra_opset):
        try:
            import tensorflow_text   # pylint: disable=import-outside-toplevel
        except ModuleNotFoundError:
            logger.warning("tensorflow_text not installed. Model will fail to load if tensorflow_text ops are used.")

    # get the frozen tensorflow model from graphdef, checkpoint or saved_model.
    if args.graphdef:
        graph_def, inputs, outputs = tf_loader.from_graphdef(args.graphdef, args.inputs, args.outputs)
        model_path = args.graphdef
    if args.checkpoint:
        graph_def, inputs, outputs = tf_loader.from_checkpoint(args.checkpoint, args.inputs, args.outputs)
        model_path = args.checkpoint
    if args.saved_model:
        graph_def, inputs, outputs, initialized_tables, tensors_to_rename = tf_loader.from_saved_model(
            args.saved_model, args.inputs, args.outputs, args.tag, args.signature_def, args.concrete_function,
            args.large_model, return_initialized_tables=True, return_tensors_to_rename=True)
        model_path = args.saved_model
    if args.keras:
        graph_def, inputs, outputs = tf_loader.from_keras(
            args.keras, args.inputs, args.outputs)
        model_path = args.keras
    if args.tflite:
        graph_def = None
        inputs = None
        outputs = None
        tflite_path = args.tflite
        model_path = tflite_path

    if args.verbose:
        logger.info("inputs: %s", inputs)
        logger.info("outputs: %s", outputs)

    if args.rename_inputs:
        tensors_to_rename.update(zip(inputs, args.rename_inputs))
    if args.rename_outputs:
        tensors_to_rename.update(zip(outputs, args.rename_outputs))

    tf_graph = None
    const_node_values = None
    if graph_def is not None:
        with tf.Graph().as_default() as tf_graph:
            const_node_values = None
            if args.large_model:
                const_node_values = compress_graph_def(graph_def)
            if args.output_frozen_graph:
                utils.save_protobuf(args.output_frozen_graph, graph_def)
            tf.import_graph_def(graph_def, name='')

    with tf_loader.tf_session(graph=tf_graph):
        g = process_tf_graph(tf_graph,
                             continue_on_error=args.continue_on_error,
                             target=args.target,
                             opset=args.opset,
                             custom_op_handlers=custom_ops,
                             extra_opset=extra_opset,
                             shape_override=args.shape_override,
                             input_names=inputs,
                             output_names=outputs,
                             inputs_as_nchw=args.inputs_as_nchw,
                             ignore_default=args.ignore_default,
                             use_default=args.use_default,
                             const_node_values=const_node_values,
                             tensors_to_rename=tensors_to_rename,
                             initialized_tables=initialized_tables,
                             tflite_path=tflite_path,
                             dequantize=args.dequantize)

    onnx_graph = optimizer.optimize_graph(g)

    tensor_storage = ExternalTensorStorage() if args.large_model else None
    model_proto = onnx_graph.make_model("converted from {}".format(model_path), external_tensor_storage=tensor_storage)

    # write onnx graph
    logger.info("")
    logger.info("Successfully converted TensorFlow model %s to ONNX", model_path)
    logger.info("Model inputs: %s", onnx_graph.input_names)
    logger.info("Model outputs: %s", onnx_graph.outputs)
    if args.output:
        if args.large_model:
            utils.save_onnx_zip(args.output, model_proto, tensor_storage)
            logger.info("Zipped ONNX model is saved at %s. Unzip before opening in onnxruntime.", args.output)
        else:
            utils.save_protobuf(args.output, model_proto)
            logger.info("ONNX model is saved at %s", args.output)
    else:
        logger.info("To export ONNX model to file, please run with `--output` option")


if __name__ == "__main__":
    main()
