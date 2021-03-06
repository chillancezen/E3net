#! /usr/bin/python3
import json
import ast
import concurrent.futures
from e3net.e3common.E3MQ import E3MQClient
from e3net.e3common.E3LOG import get_e3loger
from e3net.e3compute.E3Container import get_e3container_by_id ,set_e3container_status
from e3net.e3compute.E3Container import set_e3container_extra, set_e3container_host
from e3net.e3compute.E3Container import set_e3container_running_status,unregister_e3container_post
from e3net.e3compute.DBCompute import init_e3compute_database
from e3net.e3compute.E3COMPUTEHost import get_e3image_by_id
from e3net.e3compute.E3COMPUTEHost import get_e3flavor_by_id
from e3net.e3compute.E3COMPUTEHost import get_e3host_by_name
from e3net.e3compute.E3ComputeEtcd import *

e3log=get_e3loger('e3scheduler')

'''
scheduler=E3MQClient(queue_name='e3-scheduler-mq',
                        user='e3net',
                        passwd='e3credentials')
scheduler_anothrt=E3MQClient(queue_name='e3-scheduler-another-mq',
                        user='e3net',
                        passwd='e3credentials')
'''
hostmq=None #=E3MQClient(queue_name=None,user='e3net',passwd='e3credentials')

sequential_executor=concurrent.futures.ThreadPoolExecutor(max_workers=1)
concurrent_executor=concurrent.futures.ThreadPoolExecutor(max_workers=5)

#hostagent message queue format:host-<uuid>

def assign_host_side_mq(mq):
    global hostmq
    hostmq=mq

def _validate_image_and_flavor(data):
    image=data['image']
    flavor=data['flavor']
    flavor_size=flavor.disk*1024*1024*1024
    image_size=image.size
    if image_size > flavor_size:
        return False
    return True

def boot_container_bottom_half(data):
    
    container=data['container']
    image=data['image']
    flavor=data['flavor']

    if _validate_image_and_flavor(data) is False:
        error_msg='image(name:%s)\'s size is larger than flavor(name:%s)\'s size'%(image.name,flavor.name)
        e3log.error(error_msg)
        set_e3container_extra(container.id,error_msg)
        set_e3container_status(container.id,'failed')
        return
 
    #to-do:select an host according to the flavor(compute)&network requirment 
    #and run the container on the target host
    #here we still randomly choose one host
    #allocate the network resource
    #allocate the compute resource
    
    #for debug purpose ,here we manually select one host
    host=get_e3host_by_name('nfv-volume')

    set_e3container_host(container.id,host.name)
    #1 allocate memory/cpu/disk from host
    error_flag=False
    if etcd_allocate_memory(host.id,flavor.mem) is True:
        if etcd_get_free_disk(host.id) < flavor.disk:
            error_flag=True
            etcd_deallocate_memory(host.id,flavor.mem)
        elif etcd_allocate_cpus_for_container(host.id,container.id,flavor.cpus) is False:
            error_flag=True
            etcd_deallocate_memory(host.id,flavor.mem)
    else:
        error_flag=True

    if error_flag is True:
        error_msg='can not satisify the allocation requirement(cpu:%s(free:%s) memory:%sMB(free:%sMB) disk:%sGB(free:%sGB)) of container(id:%s)'%(
                flavor.cpus,
                len(etcd_get_free_cpus(host.id)),
                flavor.mem,
                etcd_get_free_memory(host.id),
                flavor.disk,
                etcd_get_free_disk(host.id),
                container.id)
        e3log.error(error_msg)
        set_e3container_extra(container.id,error_msg)
        set_e3container_status(container.id,'failed')
        return
    #2 send task to agent to boot the container 
    mq_id='host-%s'%(host.id)
    msg=dict()
    msg['action']='boot'
    msg['body']=str(data)
    rc=hostmq.enqueue_message(msg=str(msg),queue_another=mq_id)
    if rc:
        e3log.info('distributing container(id:%s) booting message to host(name:%s) succeeds'%(container.id,host.name))
    else:
        set_e3container_status(container.id,'failed')
        set_e3container_extra(container.id,'distributing container booting message to host(name:%s) fails'%(containerhost.name)) 
        e3log.error('distributing container(id:%s) booting message to host(name:%s) fails'%(container.id,host.name))

def boot_container(msg):
    if 'container_id' not in msg:
        return 
    container_id=msg['container_id']
    container=get_e3container_by_id(container_id)
    if not container:
        e3log.error('can not find container by id:%s to boot'%(container_id))
        return
    image=get_e3image_by_id(container.image_id)
    flavor=get_e3flavor_by_id(container.flavor_id)
    if not image or not flavor :
        error_msg='can not find image or flavor associated with container(id:%s)'%(container_id)
        e3log.error('can not find image or flavor associated with container(id:%s)'%(container_id))
        return
    #1. check the task status of the target container
    if container.task_status != 'created':
        e3log.error('can not boot container(id:%s) since its task status is:%s'%(container_id,container.task_status))
        return
    rc=set_e3container_status(container.id,'spawning')
    if rc:
        e3log.info('container(id:%s) is spawning'%(container_id))
    else:
        e3log.error('can not set container(id:%s) to status:spawning,task terminated'%(container_id)) 
        return
    try:
        sequential_executor.submit(boot_container_bottom_half,{'container':container,'image':image,'flavor':flavor})
    except:
        e3log.error('errors occur when submitting bottom half task of booting container(id:%s)'%(container_id))


def start_container(msg):
    if 'container_id' not in msg:
        return
    container_id=msg['container_id']
    container=get_e3container_by_id(container_id)
    if not container:
        e3log.error('can not find container by id:%s to start it'%(container_id))
        return
    host=get_e3host_by_name(container.host)
    if not host:
        e3log.error('can not find host by name:%s to start continer'%(container.host))
        return
    mq_id='host-%s'%(host.id)
    _msg=dict()
    _msg['action']='start'
    _msg['body']={'container_id':container.id}
    rc=hostmq.enqueue_message(msg=str(_msg),queue_another=mq_id)
    if rc is True:
        e3log.info('distributing container(id:%s) starting message to host(name:%s) succeeds'%(container.id,host.name))
    else:
        e3log.error('distributing container(id:%s) starting message to host(name:%s) fails'%(container.id,host.name))

def stop_container(msg):
    if 'container_id' not in msg:
        return
    container_id=msg['container_id']
    container=get_e3container_by_id(container_id)
    if not container:
        e3log.error('can not find container by id:%s to stop it'%(container_id))
        return
    host=get_e3host_by_name(container.host)
    if not host:
        e3log.error('can not find host by name:%s to stop continer'%(container.host))
        return
    mq_id='host-%s'%(host.id)
    _msg=dict()
    _msg['action']='stop'
    _msg['body']={'container_id':container.id}
    rc=hostmq.enqueue_message(msg=str(_msg),queue_another=mq_id)
    if rc is True:
        e3log.info('distributing container(id:%s) stopping message to host(name:%s) succeeds'%(container.id,host.name))
    else:
        e3log.error('distributing container(id:%s) stopping message to host(name:%s) fails'%(container.id,host.name)) 

def destroy_container(msg):
    if 'container_id' not in msg:
        return
    container_id=msg['container_id']
    container=get_e3container_by_id(container_id)
    if not container:
        e3log.error('can not find container by id:%s to destroy it'%(container_id))
        return
    host=get_e3host_by_name(container.host)
    if not host:
        e3log.error('can not find host by name:%s to destroy continer'%(container.host))
        return
    mq_id='host-%s'%(host.id)
    _msg=dict()
    _msg['action']='destroy'
    _msg['body']={'container_id':container.id}
    rc=hostmq.enqueue_message(msg=str(_msg),queue_another=mq_id)
    if rc is True:
        e3log.info('distributing container(id:%s) destroying message to host(name:%s) succeeds'%(container.id,host.name))
    else:
        e3log.error('distributing container(id:%s) destroying message to host(name:%s) fails'%(container.id,host.name))

def notify_boot_func(body):
    container_id=body['container_id']
    status=body['status']
    
    if status=='OK':
        if set_e3container_status(container_id,'deployed') is False:
            e3log.error('setting container(id:%s)\'s status to deployed fails'%(container_id))
    elif status=='FAIL':
        #reclaim the resource we allocated before
        try:
            container=get_e3container_by_id(container_id)
            flavor=get_e3flavor_by_id(container.flavor_id)
            host=get_e3host_by_name(container.host)
            etcd_deallocate_memory(host.id,flavor.mem)
            etcd_release_cpus_of_container(host.id,container.id) 
        except Exception as e:
            pass
        set_e3container_status(container_id,'failed')
        if 'msg' in body:
            set_e3container_extra(container_id,body['msg'])
   
            
def notify_start_func(body):
    container_id=body['container_id']
    status=body['status']
    
    if status=='OK':
        set_e3container_running_status(container_id,'running')
    elif status=='FAIL':
        set_e3container_running_status(container_id,'stopped')

def notify_stop_func(body):
    container_id=body['container_id']
    status=body['status']
    if status=='OK':
        set_e3container_running_status(container_id,'stopped')
    elif status=='FAIL':
        set_e3container_running_status(container_id,'running')

def notify_destory_func(body):
    container_id=body['container_id']
    status=body['status']
    container=get_e3container_by_id(container_id)
    if not container:
        e3log.warn('can not find the container(id:%s) when releasing container in notification phaze'%(container_id))
        return
    host=get_e3host_by_name(container.host)
    if not host:
        e3log.warn('can not find the host(id:%s) when releasing container in notification phaze'%(container_id))
        return
    #1 release cpus resource right now
    etcd_release_cpus_of_container(host.id,container.id)
    #2 release memory resource 
    flavor=get_e3flavor_by_id(container.flavor_id)
    if flavor:
        etcd_deallocate_memory(host.id,flavor.mem)
    #3 delete database
    if unregister_e3container_post(container.id) is False:
        e3log.error('error occurs during deleting database entry for container(id:%s) fails'%(container.id))
    else:
        e3log.info(' deleting database entry for container(id:%s) succeeds'%(container.id))

notify_dist_table={
    'boot':notify_boot_func,
    'start':notify_start_func,
    'stop':notify_stop_func,
    'destroy':notify_destory_func,
}

def notify_controller(msg):
    body=msg['body']
    if 'prior_action' not in body:
        return
    prior_action=body['prior_action']
    if prior_action not in notify_dist_table:
        return
    notify_dist_table[prior_action](body)
 

dist_table={
'boot':boot_container,
'start':start_container,
'stop':stop_container,
'destroy':destroy_container
}

another_dist_table={
'notify':notify_controller
}
def e3scheduler_func(ch,method,properties,body):
    try:
        msg=json.loads(body.decode("utf-8"))
    except:
        return
    if 'action' not in msg:
        return 
    action=msg['action']
    if action not in dist_table:
        return 
    dist_table[action](msg)
    #if distributed routine does not race with other action, submit to consurrent worker
def e3_scheduler_another_func(ch,method,properties,body):
    try:
        msg=ast.literal_eval(body.decode("utf-8"))
    except:
        return
    if 'action' not in msg:
        return
    action=msg['action']
    if action not in another_dist_table:
        return
    another_dist_table[action](msg)

if __name__=='__main__':
    pass
    '''
    print('init etcd:',init_etcd_session(etcd_ip='10.0.2.15',etcd_port=2379))
    assign_host_side_mq(E3MQClient(queue_name=None,user='e3net',passwd='e3credentials'))
    init_e3compute_database('mysql+pymysql://e3net:e3credientials@localhost/E3compute')
    scheduler.start_dequeue(e3scheduler_func)
    '''
