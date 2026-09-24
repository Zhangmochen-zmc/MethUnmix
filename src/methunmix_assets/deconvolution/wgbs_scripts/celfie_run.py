#!/usr/bin/env python

import argparse
import os
import sys

import bottleneck as bn  # substantially speeds up calculations with nan's
import numpy as np

np.random.seed(int(os.environ.get("DEMETHFLOW_RANDOM_SEED", "0")))
import pandas as pd
import pickle as pkl  # to save output

np.seterr(divide="ignore", invalid="ignore")

################  support functions   ################

def add_pseudocounts(value, array, meth, meth_depths):
    axis0, axis1 = np.where(array == value)
    meth[axis0, axis1] += 1
    meth_depths[axis0, axis1] += 2

def check_gamma(array):
    return (0 in array) or (1 in array)

########  expectation-maximization algorithm  ########

def expectation(gamma, alpha):
    alpha = alpha.T[:, np.newaxis, :]
    gamma = gamma[..., np.newaxis]
    p0 = (1.0 - gamma) * alpha
    p1 = gamma * alpha
    p0 /= np.nansum(p0, axis=0)[np.newaxis, ...]
    p1 /= np.nansum(p1, axis=0)[np.newaxis, ...]
    return p0, p1

def log_likelihood(p0, p1, x_depths, x, y_depths, y, gamma, alpha):
    alpha = alpha.T[:, np.newaxis, :]
    gamma = gamma[..., np.newaxis]
    y = y[..., np.newaxis]
    y_depths = y_depths[..., np.newaxis]
    x = x.T[np.newaxis, ...]
    x_depths = x_depths.T[np.newaxis, ...]
    ll = np.sum((y + p1 * x) * np.log(gamma))
    ll += np.sum((y_depths - y + p0 * (x_depths - x)) * np.log(1.0 - gamma))
    ll += np.sum((p1 * x + (x_depths - x) * p0) * np.log(alpha))
    return ll

def maximization(p0, p1, x, x_depths, y, y_depths):
    individuals = p0.shape[2]
    ones_vector = np.ones(shape=(y.shape[0]))
    new_alpha = np.zeros((x.shape[0], y.shape[0]))
    p0 = np.nan_to_num(p0)
    p1 = np.nan_to_num(p1)
    x = np.nan_to_num(x)
    x_depths = np.nan_to_num(x_depths)
    term0 = 0
    term1 = 0
    for n in range(individuals):
        new_alpha[n, :] = np.dot(p1[:, :, n], x[n, :]) + np.matmul(
            p0[:, :, n], (x_depths[n, :] - x[n, :])
        )
        term1 += p1[:, :, n] * (np.outer(ones_vector, x[n, :]))
        term0 += p0[:, :, n] * (np.outer(ones_vector, x_depths[n, :] - x[n, :]))
    gamma = (term1 + y) / (term0 + term1 + y_depths)
    if check_gamma(gamma):
        add_pseudocounts(1, gamma, y, y_depths)
        add_pseudocounts(0, gamma, y, y_depths)
        gamma = (term1 + y) / (term0 + term1 + y_depths)
    normalized_new_alpha = new_alpha / np.sum(new_alpha, axis=1)[:, np.newaxis]
    return normalized_new_alpha, gamma

def em(x, x_depths, y, y_depths, num_iterations, convergence_criteria):
    alpha = np.random.uniform(size=(x.shape[0], y.shape[0]))
    alpha /= np.sum(alpha, axis=1)[:, np.newaxis]
    add_pseudocounts(1, np.nan_to_num(y / y_depths), y, y_depths)
    add_pseudocounts(0, np.nan_to_num(y / y_depths), y, y_depths)
    gamma = y / y_depths
    for i in range(num_iterations):
        p0, p1 = expectation(gamma, alpha)
        a, g = maximization(p0, p1, x, x_depths, y, y_depths)
        alpha_diff = np.mean(abs(a - alpha)) / np.mean(abs(alpha))
        gamma_diff = np.mean(abs(g - gamma)) / np.mean(abs(gamma))
        if (alpha_diff + gamma_diff < convergence_criteria):
            break
        alpha, gamma = a, g
    ll = log_likelihood(p0, p1, x_depths, x, y_depths, y, gamma, alpha)
    return alpha, gamma, ll

################## read in data #######################

def define_arrays(sample, num_samples, num_unk):
    # 【核心修复】：根据 3 列坐标进行切片
    # Mixtures: 从索引 3 到 (num_samples * 2) + 3
    test = sample.iloc[:, 3 : (num_samples * 2) + 3].values.T
    
    # Reference (Atlas): 紧接着 Mixtures 之后开始，不再额外跳过 3 列
    # 【修改处】：去掉了多余的 + 3
    train = sample.iloc[:, (num_samples * 2) + 3 :].values.T

    x = test[::2, :]
    x_depths = test[1::2, :]

    y = train[::2, :]
    y_depths = train[1::2, :]

    unknown = np.zeros((num_unk, y_depths.shape[1]))
    y_depths_unknown = np.append(y_depths, unknown, axis=0)
    y_unknown = np.append(y, unknown, axis=0)

    return (np.nan_to_num(x), np.nan_to_num(x_depths), 
            np.nan_to_num(y_unknown), np.nan_to_num(y_depths_unknown))

def parse_header_names(header):
    parsed_header = []
    for i in range(0, len(header), 2):
        parts = header[i].split("_")
        if len(parts) >= 2:
            name = "_".join(parts[:2])
        else:
            name = parts[0]
        parsed_header.append(name)
    return parsed_header

def get_header(sample, num_samples, num_unk):
    header = list(sample)
    # Samples 还是从第 3 列开始
    samples = parse_header_names(header[3 : (num_samples * 2) + 3])
    # Tissues (Atlas) 紧接着 Samples 开始
    # 【修改处】：去掉了多余的 + 3
    tissues = parse_header_names(header[(num_samples * 2) + 3 :])
    unknowns = ["unknown" + str(i) for i in range(1, num_unk + 1)]
    return samples, tissues + unknowns

def write_output(output_file, output_matrix, header, index):
    output = pd.DataFrame(output_matrix)
    output.columns = header
    output.insert(0, "", index)
    output.to_csv(output_file, sep="\t", index=False)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CelFiE - Cell-free DNA decomposition.")
    parser.add_argument("input_path", help="the path to the input file")
    parser.add_argument("output_directory", help="the path to the output directory")
    parser.add_argument("num_samples", type=int, help="Number of cfdna samples")
    parser.add_argument("-m", "--max_iterations", default=1000, type=int)
    parser.add_argument("-u", "--unknowns", default=0, type=int)
    parser.add_argument("-p", "--parallel_job_id", default=1, type=int)
    parser.add_argument("-c", "--convergence", default=0.0001, type=float)
    parser.add_argument("-r", "--random_restarts", default=10, type=int)
    args = parser.parse_args()

    if not os.path.exists(args.output_directory):
        os.makedirs(args.output_directory)
    
    print(f"writing to {args.output_directory}/")
    data_df = pd.read_csv(args.input_path, delimiter="\t")
    print(f"finished reading {args.input_path}")

    output_alpha_file = f"{args.output_directory}/{args.parallel_job_id}_tissue_proportions.txt"
    output_gamma_file = f"{args.output_directory}/{args.parallel_job_id}_methylation_proportions.txt"

    x, x_depths, y, y_depths = define_arrays(data_df, int(args.num_samples), int(args.unknowns))
    samples, tissues = get_header(data_df, args.num_samples, args.unknowns)

    random_restarts = []
    for i in range(args.random_restarts):
        print(f"Random restart {i+1}/{args.random_restarts}...")
        alpha, gamma, ll = em(x, x_depths, y, y_depths, args.max_iterations, args.convergence)
        random_restarts.append((ll, alpha, gamma))

    valid_restarts = [
        item for item in random_restarts
        if np.asarray(item[0]).size == 1 and np.isfinite(float(np.asarray(item[0]).reshape(-1)[0]))
    ]
    if not valid_restarts:
        raise RuntimeError("all CelFiE random restarts produced a non-finite likelihood")
    ll_max, alpha_max, gamma_max = max(
        valid_restarts,
        key=lambda item: float(np.asarray(item[0]).reshape(-1)[0]),
    )
    write_output(output_alpha_file, alpha_max, tissues, samples)
    write_output(output_gamma_file, gamma_max.T, tissues, list(range(gamma_max.shape[1])))
    print("Deconvolution finished successfully.")
