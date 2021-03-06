import numpy as np
from garl.main_utils import mpi_print
from garl.eval import eval_set
import wandb,joblib
from garl import setup_utils

class TaskOptimizer(object):
    def __init__(self,env,rep=3,eval_limit=1e6,log=True):
        self.env = env
        self.iter = 0
        self.is_log = log
        self.rep = rep
        self.eval_steps = 0
        self.eval_limit = eval_limit
        self.hist = []

        self.size = 0
        self.train_set_size = 0

    def gen(self):
        pass

    def select(self):
        pass

    def calFit(self):
        pass

    def run(self):
        pass

    def reset(self):
        self.hist = []
        self.train_set_size = 0
        self.iter = 0
        self.eval_steps = 0


class SeedOptimizer(TaskOptimizer):
    def __init__(self,logdir,env,rand_seed=None,rep=3,
                 eval_limit=1e6,train_set_limit=500,
                 spare_size=10000,ini_size=100,log=True,load_seed=False):
        TaskOptimizer.__init__(self, env)
        #super(TaskOptimizer, self).__init__(env)
        self.rep = rep
        self.eval_limit = eval_limit

        self.spare_size = spare_size
        self.ini_size = ini_size
        self.rs = np.random.RandomState(rand_seed)
        seed_set = self.rs.randint(0,2**31-1,self.spare_size)

        self.spare_set = set(seed_set[:self.spare_size-self.ini_size])
        self.isload = load_seed
        if not load_seed:
            self.ini_set = set(seed_set[self.spare_size-self.ini_size:])
            self.hist.append(list(self.ini_set))
        else:
            self.load('opt_hist')
            self.ini_set = self.hist[-1]

        self.env.set_seed(self.ini_set)
        print("train_task/seed_optimizer/set ini_set",self.ini_set)
        # all used seed set
        self.train_set_hist = set(seed_set)
        self.train_set_size = self.ini_size
        self.train_set_limit = train_set_limit

        # If use diversity, phi = 0
        self.phi = 0
        self.iter = 0
        self.step_elapsed = 0
        self.train_rew = 0
        self.if_log = log
        self.logdir = logdir
        self.log()

    @classmethod
    def set_to_list(a):
        return list(a).sort()

    def calDiv(self,p,vec):
        assert type(p) is float
        vec1 = np.ones_like(vec) * p

        return np.sqrt(np.sum(np.square(vec1 - vec2)))

    def calFit(self,vec1):
        if self.phi != 0:
            div1 = np.zeros_like(vec1)
            for i in range(vec1.shape[0]):
                div1[i] = self.calDiv(vec1[i],vec1)
        else:
            div1 = np.zeros_like(vec1)

        fit1 = vec1 + self.phi * div1
        return fit1

    def gen(self,pop_size=1):
        # without put back to avoid error in remove
        child = np.random.choice(
                    list(self.spare_set),
                    pop_size,
                    replace=False
                )
        return list(child)

    def replace(self,sess):
        last_set = list(self.env.get_seed())
        last_set_rew = self.eval(sess,last_set,self.rep)

        curr_set = self.gen(last_set)
        curr_set_rew = self.eval(sess,curr_set,self.rep)

        next_set = []
        last_fit = self.calFit(last_set_rew)
        curr_fit = self.calFit(curr_set_rew)

        for idxs in range(len(last_set)):
            if last_fit[idxs] > curr_fit[idxs]:
                # score decrease means diffculty increase
                next_set.append(curr_set[idxs])
            else:
                next_set.append(last_set[idxs])
        self.train_set_hist.union(set(next_set))
        self.train_set_size = len(self.train_set_hist)
        self.step_elapsed += self.eval_steps

        return next_set

    def select(self,sess,ratio=0.5):
        """select 50% best"""
        last_set = list(self.env.get_seed())
        last_set_rew = self.eval(sess,last_set,self.rep)

        curr_set = self.gen(last_set)
        curr_set_rew = self.eval(sess,curr_set,self.rep)

        next_set = []
        last_fit = self.calFit(last_set_rew)
        curr_fit = self.calFit(curr_set_rew)

        fits = np.concatenate((curr_fit,last_fit),0)
        curr_set.extend(last_set)

        # [1,2,3..] ascend
        rank_fit = np.argsort(fits)
        for idxs, pos in enumerate(rank_fit):
            # to get rank 1 index of last_set
            next_set.append(curr_set[pos])
            if idxs > ratio * len(set(curr_set)):
                break

        self.train_set_hist = self.train_set_hist.union(set(next_set))
        self.train_set_size = len(self.train_set_hist)

        self.step_elapsed += self.eval_steps
        return next_set

    def add(self,sess,ratio=0.5):
        """If new level is  more difficult than average difficulty, add it
        until add 50% * now set size"""
        last_set = list(self.env.get_seed())
        curr_set = self.gen(len(last_set))

        curr_set_rew = self.eval(sess,curr_set,self.rep)
        print('/run/curr set mean_scores',curr_set_rew)
        curr_fit = self.calFit(curr_set_rew)

        next_set = last_set.copy()

        # Rank
        def should_add():
            #print(curr_fit[idx],self.train_rew)
            if curr_fit[idx] > self.train_rew:
                return False
            if len(next_set) >= (1+ratio)*len(last_set):
                return False
            if len(next_set) >= self.train_set_limit:
                return False

            return True

        for i,idx in enumerate(np.argsort(curr_fit)):
            if should_add():
                next_set.append(curr_set[idx])
                self.spare_set.remove(curr_set[idx])

        w = self.calWeight(next_set,ratio)
        self.train_set_hist = set(next_set)
        self.train_set_size = len(self.train_set_hist)
        self.step_elapsed += self.eval_steps

        return next_set,w

    def load(self,filename):
        opt_hist = joblib.load(setup_utils.file_to_path(filename))
        for key in opt_hist.keys():
            setattr(self,key,opt_hist[key])

    def log(self):
        opt_hist = {
            'step_elapsed':self.step_elapsed,
            'iter':self.iter,
            'eval_steps':self.eval_steps,
            'train_set_size':self.train_set_size,
            'hist':self.hist
        }
        if self.if_log:
            wandb.log(opt_hist)

        #idx = int(self.step_elapsed // 1e6)
        joblib.dump(opt_hist, self.logdir + "opt_hist")

    def eval(self,sess,eval_list,rep):
        nenv = self.env.num_envs
        eval_logs, eval_steps = eval_set(sess,nenv,eval_list,rep_count=rep)
        self.eval_steps +=  eval_steps

        mean_scores = [0] * len(eval_list)
        for i,seed in enumerate(eval_list):
            mean_scores[i] = eval_logs[str(seed)]

        return mean_scores

    def should_opt(self):
        if self.train_set_size >= self.train_set_limit:
            return False
        elif self.eval_steps >= self.eval_limit:
            return False
        elif self.isload == True:
            return False

        return True

    def calWeight(self,seeds,ratio):
        w = [0] * len(seeds)
        for i,s in enumerate(seeds):
            w[i] = 5*(1-ratio) if s in self.hist[-1] else 5
        return w

    def run(self,sess,env,step_elapsed,train_rew,mode='add'):
        self.env = env
        next_set = self.env.get_seed()
        self.eval_steps = 0
        self.step_elapsed = step_elapsed
        self.train_rew = train_rew
        print('/run/train_rew',self.train_rew)
        print('/run/eval_steps',self.eval_steps)
        print('/run/self.should_opt',self.should_opt())

        # Optimizen until eval limit reach
        while(self.should_opt()):
            if mode == 'replace':
                next_set = self.replace(sess)
                self.env.set_seed(next_set)
            elif mode == 'select':
                next_set = self.select(sess,ratio=0.5)
                self.env.set_seed(next_set)
            elif mode =='add':
                next_set,w = self.add(sess,ratio=0.2)
                print("weight is w",w)
                self.env.set_seed(next_set,w)
                break
            else:
                raise ValueError

        if not self.isload:
            next_set = self.env.get_seed()

            # Output nextset to be trained on
            self.hist.append(next_set)
            mpi_print("Next_set after optimizing",next_set)
            self.iter += 1
            self.log()

        return self.env, self.step_elapsed
