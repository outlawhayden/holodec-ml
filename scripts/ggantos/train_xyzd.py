import sys
from sklearn.preprocessing import StandardScaler, MinMaxScaler, MaxAbsScaler, RobustScaler
from sklearn.metrics import mean_absolute_error, max_error
import pandas as pd
import numpy as np
import argparse
import random
import yaml
import os
from os.path import join, exists
from datetime import datetime
import tensorflow as tf

sys.path.append('../../')
    
from holodecml.data import load_scaled_datasets
from holodecml.models import Conv2DNeuralNetwork


scalers = {"MinMaxScaler": MinMaxScaler,
           "MaxAbsScaler": MaxAbsScaler,
           "StandardScaler": StandardScaler,
           "RobustScaler": RobustScaler}

metrics = {"mae": mean_absolute_error}


def main():
        
    # parse arguments from config/yaml file
    parser = argparse.ArgumentParser(description='Describe a Conv2D nn')
    parser.add_argument("config", help="Path to config file")
    args = parser.parse_args()
    with open(args.config) as config_file:
        config = yaml.load(config_file, Loader=yaml.FullLoader)

    path_data = config["path_data"]
    path_save = config["path_save"]
    if not os.path.exists(path_save):
        os.makedirs(path_save)
    num_particles = config["num_particles"]
    output_cols = config["output_cols"]
    seed = config["random_seed"]
    np.random.seed(seed)
    random.seed(seed)
    tf.random.set_seed(seed)
    
    # load data
    scaler_out = scalers[config["scaler_out"]]()
    load_start = datetime.now()
    train_inputs,\
    train_outputs,\
    valid_inputs,\
    valid_outputs = load_scaled_datasets(path_data,
                                         num_particles,
                                         output_cols,
                                         scaler_out,
                                         config["subset"],
                                         config["num_z_bins"],
                                         config["mass"])
    print(f"Loading datasets took {datetime.now() - load_start} time")
    
    # train and save the model
    model_start = datetime.now()
    mod = Conv2DNeuralNetwork(**config["conv2d_network"])
    hist = mod.fit(train_inputs, train_outputs,
                   xv=valid_inputs, yv=valid_outputs)
    print(f"Running model took {datetime.now() - model_start} time")
    
    # predict outputs
    train_outputs_pred = mod.predict(train_inputs)
    valid_outputs_pred = mod.predict(valid_inputs)
    
    # apply inverse scaler to outputs
    train_outputs_pred_raw = scaler_out.inverse_transform(train_outputs_pred)       
    valid_outputs_pred_raw = scaler_out.inverse_transform(valid_outputs_pred)
    
    # calculate error
    valid_maes = np.zeros(len(output_cols))
    valid_maxerror = np.zeros(len(output_cols))
    for col in range(len(output_cols)):
        valid_maes[col] = mean_absolute_error(valid_outputs[col],
                                              valid_outputs_pred[col])
        valid_maxerror[col] = max_error(valid_outputs[col],
                                        valid_outputs_pred[col])
    
    # save results
    print("Saving results and config file..")
    mod.model.save(join(path_save, config["model_name"]+".h5"))
    np.savetxt(join(path_save, "train_outputs_pred.csv"), train_outputs_pred)
    np.savetxt(join(path_save, "train_outputs_pred_raw.csv"),
               train_outputs_pred_raw)
    np.savetxt(join(path_save, "valid_outputs_pred.csv"), valid_outputs_pred)   
    np.savetxt(join(path_save, "valid_outputs_pred_raw.csv"),
               valid_outputs_pred_raw)
    np.savetxt(join(path_save, "valid_maes.csv"), valid_maes)    
    np.savetxt(join(path_save, "valid_maxerror.csv"), valid_maxerror)    
    for k in hist.keys():
        np.savetxt(join(path_save, k+".csv"), hist[k])
    with open(join(path_save, 'config.yml'), 'w') as f:
        yaml.dump(config, f)
                     
    return

if __name__ == "__main__":
    main()
    
