import time
import os
import torch
import torch.nn.functional as F
from reconstruction import Regressor

def loss_function(original, reconstruction, mu, log_var, beta):
    reconstruction_loss = F.l1_loss(reconstruction, original, reduction='mean')
    kld_loss = torch.mean(-0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1), dim = 0)
    #kld_loss = torch.nan_to_num(kld_loss)

    return reconstruction_loss + beta*kld_loss

def run(model, train_loader, test_loader, epochs, optimizer, scheduler, writer,
        device, beta, w_cls, guided, clip_value):
    
    model_c = Regressor().to(device)
    optimizer_c = torch.optim.Adam(model_c.parameters(), lr=1e-3, weight_decay=0)

    min_train_loss, min_test_loss = 99999, 99999

    for epoch in range(1, epochs + 1):
        t = time.time()
        train_loss = train(model, optimizer, model_c, optimizer_c, train_loader, device, beta, w_cls, guided, clip_value)
        t_duration = time.time() - t
        test_loss = test(model, test_loader, device, beta)
        scheduler.step()
        info = { 'current_epoch': epoch, 'epochs': epochs, 'train_loss': train_loss, 'test_loss': test_loss, 't_duration': t_duration }

        min_train_loss = min_train_loss if min_train_loss <= train_loss else train_loss
        min_test_loss = min_test_loss if min_test_loss <= test_loss else test_loss

        writer.print_info(info)
        writer.save_checkpoint(model, optimizer, scheduler, epoch)
        torch.save(model.state_dict(), "model_state_dict.pt")
        torch.save(model_c.state_dict(), "model_c_state_dict.pt")

    info_2 = {"epochs": float(epochs), "min_train_loss": float(min_train_loss), "min_test_loss": float(min_test_loss)}
    
    return info_2

def train(model, optimizer, model_c, optimizer_c, loader, device, beta, w_cls, guided, clip_value):
    model.train()
    model_c.train()

    total_loss = 0
    recon_loss = 0
    reg_loss = 0
    cls1_error = 0
    cls2_error = 0

    for data in loader:

        
	    # Load Data
        x = data.x.to(device)
        label = data.y.to(device)

        #print(f"{x}          {label}")
        #x = torch.nan_to_num(x)
        #print(torch.isnan(x).any())
        #print(torch.where(torch.isnan(x)))

	    # VAE + Excitation
        optimizer.zero_grad()
        out, mu, log_var, re = model(x)

        #print(f"{out}   \n  {mu}    \n    {log_var}    \n    {re}")        

        loss = loss_function(x, out, mu, log_var, beta)       
        if guided:
            loss_cls = F.mse_loss(re, label, reduction='mean')
            loss += loss_cls * w_cls
        loss.backward()  
        torch.nn.utils.clip_grad_value_(model.parameters(), clip_value=clip_value)      
        optimizer.step()
        total_loss += loss.item()

        #print(total_loss)

        #exit() #$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$

        if guided:
            # Inhibition Step 1
            optimizer_c.zero_grad()
            z = model.reparameterize(mu, log_var).detach()
            z = z[:, 1:]
            cls1 = model_c(z)
            loss = F.mse_loss(cls1, label, reduction='mean')
            cls1_error += loss.item()
            loss *= w_cls
            loss.backward()
            torch.nn.utils.clip_grad_value_(model_c.parameters(), clip_value=clip_value)
            optimizer_c.step()

            # Inhibition Step 2
            optimizer.zero_grad()
            mu, log_var = model.encoder(x)
            z = model.reparameterize(mu, log_var)
            z = z[:, 1:]
            cls2 = model_c(z)
            label1 = torch.empty_like(label).fill_(0.5)
            loss = F.mse_loss(cls2, label1, reduction='mean')
            cls2_error += loss.item()
            loss *= w_cls
            loss.backward()
            torch.nn.utils.clip_grad_value_(model_c.parameters(), clip_value=clip_value)
            optimizer.step()
    
    return total_loss / len(loader)


def test(model, loader, device, beta):
    model.eval()
    model.training = False

    total_loss = 0
    recon_loss = 0
    reg_loss = 0
    with torch.no_grad():
        for i, data in enumerate(loader):
            x = data.x.to(device)
            
            #print(torch.isnan(x).any())

            y = data.y.to(device)
            pred, mu, log_var, re = model(x)
            total_loss += loss_function(x, pred, mu, log_var, beta)
            recon_loss += F.l1_loss(pred, x, reduction='mean')
            reg_loss += F.mse_loss(re, y, reduction='mean')
           
            #print(f"{pred}    \n   {mu}    \n    {log_var}   \n    {re}     \n    {total_loss}    \n   {recon_loss}      \n    {reg_loss}")
            #print(torch.mean(-0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1), dim = 0))
            #print(x)
            #print(log_var)
            #print(log_var.exp())
            #print(torch.isnan(x).any())

            #exit()

    return total_loss / len(loader)


def eval_error(model, test_loader, device, meshdata, out_dir):
    model.eval()
    model.training = False

    errors = []
    mean = meshdata.mean
    std = meshdata.std + 0.000000001
    with torch.no_grad():
        for i, data in enumerate(test_loader):
            x = data.x.to(device)
            # pred = model(x)
            pred, mu, log_var, re = model(x)
            num_graphs = data.num_graphs
            reshaped_pred = (pred.view(num_graphs, -1, 1).cpu() * std) + mean
            reshaped_x = (x.view(num_graphs, -1, 1).cpu() * std) + mean

            reshaped_pred *= 300
            reshaped_x *= 300

            tmp_error = torch.sqrt(
                torch.sum((reshaped_pred - reshaped_x)**2,
                          dim=2))  # [num_graphs, num_nodes]
            errors.append(tmp_error)
        new_errors = torch.cat(errors, dim=0)  # [n_total_graphs, num_nodes]

        mean_error = new_errors.view((-1, )).mean()
        std_error = new_errors.view((-1, )).std()
        median_error = new_errors.view((-1, )).median()

    message = 'Euclidean Error: {:.3f}+{:.3f} | {:.3f}'.format(mean_error, std_error,
                                                     median_error)

    out_error_fp = out_dir + '/euc_errors.txt'
    with open(out_error_fp, 'a') as log_file:
        log_file.write('{:s}\n'.format(message))
    print("")
    print("")
    print(message)
    print("")
    print("")

    return mean_error

