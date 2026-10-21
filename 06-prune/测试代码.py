import torch
if __name__ == '__main__':
    t = torch.randint(low=1,high=5,size=(2,3,4))
    print(t.numel())